set -x

gsm8k_train_path=$HOME/data/gsm8k/train.parquet
gsm8k_test_path=$HOME/data/gsm8k/test.parquet
math_train_path=$HOME/data/math/train.parquet
math_test_path=$HOME/data/math/test.parquet
  
#model_path=$HOME/model/Qwen/Qwen2-7B-Instruct
model_path=$HOME/model/Qwen/Qwen2.5-0.5B-Instruct
  
#train_files="['$gsm8k_train_path', '$math_train_path']"
#test_files="['$gsm8k_test_path', '$math_test_path']"
  
train_files="['$gsm8k_train_path']"
test_files="['$gsm8k_test_path']"

export FLASH_ATTENTION_USE_TORCH=1
export CUDA_LAUNCH_BLOCKING=1
export TORCH_USE_CUDA_DSA=1
export HYDRA_FULL_ERROR=1
export VLLM_ATTENTION_BACKEND=XFORMERS
export VLLM_USE_V1=0

composer verl/verl/trainer/main_ppo.py \
      ++data.train_files="$train_files" \
      ++data.val_files="$test_files" \
      ++data.train_batch_size=1024 \
      ++data.max_prompt_length=1024 \
      ++data.max_response_length=512 \
      ++actor_rollout_ref.model.path="$model_path" \
      ++actor_rollout_ref.actor.optim.lr=1e-6 \
      ++actor_rollout_ref.model.use_remove_padding=True \
      ++actor_rollout_ref.actor.ppo_mini_batch_size=256 \
      ++actor_rollout_ref.actor.ppo_micro_batch_size_per_gpu=16 \
      ++actor_rollout_ref.model.enable_gradient_checkpointing=True \
      ++actor_rollout_ref.actor.fsdp_config.param_offload=False \
      ++actor_rollout_ref.actor.fsdp_config.optimizer_offload=False \
      ++actor_rollout_ref.rollout.log_prob_micro_batch_size_per_gpu=12 \
      ++actor_rollout_ref.rollout.tensor_model_parallel_size=2 \
      ++actor_rollout_ref.rollout.name=vllm \
      ++actor_rollout_ref.rollout.gpu_memory_utilization=0.5 \
      ++actor_rollout_ref.ref.log_prob_micro_batch_size_per_gpu=32 \
      ++actor_rollout_ref.ref.fsdp_config.param_offload=True \
      ++critic.optim.lr=1e-5 \
      ++critic.model.use_remove_padding=True \
      ++critic.model.path="$model_path" \
      ++critic.model.enable_gradient_checkpointing=True \
      ++critic.ppo_micro_batch_size_per_gpu=8 \
      ++critic.model.fsdp_config.param_offload=False \
      ++critic.model.fsdp_config.optimizer_offload=False \
      ++algorithm.kl_ctrl.kl_coef=0.001 \
      ++trainer.critic_warmup=0 \
      ++trainer.logger=['console','mlflow'] \
      ++trainer.project_name='verl_example' \
      ++trainer.n_gpus_per_node=8 \
      ++trainer.nnodes=1 \
      ++trainer.save_freq=-1 \
      ++trainer.test_freq=10 \
      ++trainer.total_epochs=15 
