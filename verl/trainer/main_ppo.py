# Copyright 2024 Bytedance Ltd. and/or its affiliates
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""
Note that we don't combine the main with ray_trainer as ray_trainer is used by other main.
"""
from verl.trainer.ppo.ray_trainer import RayPPOTrainer

import ray
import hydra

import os
import torch.distributed as dist
import torch
import time
import subprocess
import ray
import datetime
import socket

def get_ip():
    """Retrieve the IP address of the current node."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip_address = s.getsockname()[0]
        s.close()
        return ip_address
    except Exception:
        return "127.0.0.1"

def get_local_rank():
    """Retrieve the local rank of the process."""
    return int(os.environ.get("LOCAL_RANK", 0))

def get_global_rank():
    """Retrieve the global rank of the process."""
    return dist.get_rank() if dist.is_initialized() else 0

def initialize_ray_cluster():

    dist.init_process_group(backend="nccl", timeout=datetime.timedelta(seconds=120))

    torch.cuda.set_device(f'cuda:{get_local_rank()}')

    ip_address = get_ip()

    # Gather IP addresses from all nodes
    gathered_ips = [None] * dist.get_world_size()
    dist.all_gather_object(gathered_ips, ip_address)
    head_ip_address = gathered_ips[0]  # Use rank 0 as head

    print(f"bigning debug {gathered_ips=}, {ip_address=}, {get_global_rank()=}")

    dist.barrier()

    if get_local_rank() == 0 and get_global_rank() == 0:
        subprocess.run('ray start --head', shell=True)
        ray.init()

    dist.barrier()

    if get_local_rank() == 0 and get_global_rank() != 0:
        time.sleep(10)
        print(f"bigning debug {head_ip_address=}, {get_global_rank()=}")
        subprocess.run(f'ray start --address {head_ip_address}:6379', shell=True)
        print(f"bigning debug ray start done")
        ray.init(address=f'{head_ip_address}:6379')

    dist.barrier()

    if get_local_rank() == 0 and get_global_rank() == 0:
        result = subprocess.run('ray status', shell=True, capture_output=True, text=True)
        print(f"bigning debug {result=}")
        print("ray cluster resources")
        print(ray.cluster_resources())

    torch.distributed.destroy_process_group()

@hydra.main(config_path='config', config_name='ppo_trainer', version_base=None)
def main(config):
    run_ppo(config)


def run_ppo(config, compute_score=None):
    if not ray.is_initialized():
        # this is for local ray cluster
        ray.init(runtime_env={'env_vars': {'TOKENIZERS_PARALLELISM': 'true', 'NCCL_DEBUG': 'WARN'}})

    ray.get(main_task.remote(config, compute_score))


@ray.remote(num_cpus=1)  # please make sure main_task is not scheduled on head
def main_task(config, compute_score=None):
    from verl.utils.fs import copy_to_local
    # print initial config
    from pprint import pprint
    from omegaconf import OmegaConf
    pprint(OmegaConf.to_container(config, resolve=True))  # resolve=True will eval symbol values
    OmegaConf.resolve(config)

    # download the checkpoint from hdfs
    local_path = copy_to_local(config.actor_rollout_ref.model.path)

    # instantiate tokenizer
    from verl.utils import hf_tokenizer
    tokenizer = hf_tokenizer(local_path)

    # define worker classes
    if config.actor_rollout_ref.actor.strategy == 'fsdp':
        assert config.actor_rollout_ref.actor.strategy == config.critic.strategy
        from verl.workers.fsdp_workers import ActorRolloutRefWorker, CriticWorker
        from verl.single_controller.ray import RayWorkerGroup
        ray_worker_group_cls = RayWorkerGroup

    elif config.actor_rollout_ref.actor.strategy == 'megatron':
        assert config.actor_rollout_ref.actor.strategy == config.critic.strategy
        from verl.workers.megatron_workers import ActorRolloutRefWorker, CriticWorker
        from verl.single_controller.ray.megatron import NVMegatronRayWorkerGroup
        ray_worker_group_cls = NVMegatronRayWorkerGroup

    else:
        raise NotImplementedError

    from verl.trainer.ppo.ray_trainer import ResourcePoolManager, Role

    role_worker_mapping = {
        Role.ActorRollout: ray.remote(ActorRolloutRefWorker),
        Role.Critic: ray.remote(CriticWorker),
        Role.RefPolicy: ray.remote(ActorRolloutRefWorker)
    }

    global_pool_id = 'global_pool'
    resource_pool_spec = {
        global_pool_id: [config.trainer.n_gpus_per_node] * config.trainer.nnodes,
    }
    mapping = {
        Role.ActorRollout: global_pool_id,
        Role.Critic: global_pool_id,
        Role.RefPolicy: global_pool_id,
    }

    # we should adopt a multi-source reward function here
    # - for rule-based rm, we directly call a reward score
    # - for model-based rm, we call a model
    # - for code related prompt, we send to a sandbox if there are test cases
    # - finally, we combine all the rewards together
    # - The reward type depends on the tag of the data
    if config.reward_model.enable:
        if config.reward_model.strategy == 'fsdp':
            from verl.workers.fsdp_workers import RewardModelWorker
        elif config.reward_model.strategy == 'megatron':
            from verl.workers.megatron_workers import RewardModelWorker
        else:
            raise NotImplementedError
        role_worker_mapping[Role.RewardModel] = ray.remote(RewardModelWorker)
        mapping[Role.RewardModel] = global_pool_id

    reward_manager_name = config.reward_model.get("reward_manager", "naive")
    if reward_manager_name == 'naive':
        from verl.workers.reward_manager import NaiveRewardManager
        reward_manager_cls = NaiveRewardManager
    elif reward_manager_name == 'prime':
        from verl.workers.reward_manager import PrimeRewardManager
        reward_manager_cls = PrimeRewardManager
    else:
        raise NotImplementedError
    reward_fn = reward_manager_cls(tokenizer=tokenizer, num_examine=0, compute_score=compute_score)

    # Note that we always use function-based RM for validation
    val_reward_fn = reward_manager_cls(tokenizer=tokenizer, num_examine=1, compute_score=compute_score)

    resource_pool_manager = ResourcePoolManager(resource_pool_spec=resource_pool_spec, mapping=mapping)

    trainer = RayPPOTrainer(config=config,
                            tokenizer=tokenizer,
                            role_worker_mapping=role_worker_mapping,
                            resource_pool_manager=resource_pool_manager,
                            ray_worker_group_cls=ray_worker_group_cls,
                            reward_fn=reward_fn,
                            val_reward_fn=val_reward_fn)
    trainer.init_workers()
    trainer.fit()




if __name__ == '__main__':
    initialize_ray_cluster()

    if get_global_rank() == 0:
        main()

    #dist.barrier()
    # Destroy NCCL process group safely
    print(f"Rank {get_global_rank()} destroying NCCL process group...")
    #dist.destroy_process_group()

    print(f"Rank {get_global_rank()} successfully cleaned up.")

