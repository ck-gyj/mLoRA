import copy
import logging
from typing import List, OrderedDict, Tuple, override, Callable, Dict, Optional
from enum import Enum
import time

import torch
import os
from torch.distributions import Categorical
import json
import numpy as np
from functools import partial

from mlora.config import PPOTaskConfig
from mlora.executor.context import TRAINCONTEXT_CLASS, TrainTaskContext, TaskContext, INFERENCECONTEXT_CLASS
from mlora.model.args import LinearInfo, MLoRADataConfig, Tokens
from mlora.model.modules import AdapterModel
from mlora.model.tokenizer import Tokenizer

from .train_task import TrainTask


class Stage(Enum):
    reward_model_training = 0
    policy_training_init = 1
    policy_training_decision = 2
    policy_training_update = 3
    policy_training_iteration = 4

    # 重载 __eq__ 方法，使得可以直接比较枚举成员与数字值
    def __eq__(self, other):
        if isinstance(other, int):
            return self.value == other
        return super().__eq__(other)
     
class PPOTask(TrainTask):

    reward_context_: TrainTaskContext
    reward_tensor: Optional[torch.Tensor] = None
    critic_context_: TrainTaskContext
    critic_tensor: Optional[torch.Tensor] = None
    actor_context_: TrainTaskContext
    ref_context_: Optional[TaskContext]
    config_: PPOTaskConfig
    idx: int #generate index
    now_K_epochs: int 
    now_optim_iter_num: int
    adv: torch.Tensor
    td_target: torch.Tensor
    policy_tokens: list[list[int]]
    state_: Stage # 0: initial stage      1: decision stage       2: update state     3: iteration state


    def __init__(self, config: PPOTaskConfig, llm_name: str) -> None:
        super().__init__(config, llm_name)
        self.policy_tokens=[]
        self.state_=Stage.reward_model_training
        self.idx=1
        self.now_K_epochs=0
        self.now_optim_iter_num=0  
        self.adv=torch.zeros(1)
        self.td_target=torch.zeros(1)
        self.perm=torch.zeros(1)
        self.now_epoch_=0

    def init_tensor(self, dim: int):
        torch.manual_seed(42)
        device=self.td_target.device
        PPOTask.reward_tensor=torch.randn((dim,1),requires_grad=False,device=device)
        PPOTask.critic_tensor=torch.randn((dim,1),requires_grad=False,device=device)
        torch.manual_seed(int(time.time() * 1000) % (2**32))  # 用时间戳设置新的种子
        
    def prepare(self, linears_info: OrderedDict[str, LinearInfo],tokenizer: Tokenizer):
        self.tokenizer_ = tokenizer

        # prepare the context and the dataset
        # NOTE: how to recover the sort of dataset
        self._pre_dataset()
        self.ppo_pre_context(linears_info)

        LOSS_CLASS={"mse":partial(self.ppo_mse),"adv_loss":partial(self.ppo_adv_loss),
        "reward_loss":partial(self.ppo_reward_loss)}
        self.critic_context_.set_loss_fn(LOSS_CLASS[self.config_.critic_loss_type_])
        self.actor_context_.set_loss_fn(LOSS_CLASS[self.config_.actor_loss_type_])
        self.reward_context_.set_loss_fn(LOSS_CLASS[self.config_.reward_loss_type_])

    def _pre_ref_context(self, linears_info: OrderedDict[str, LinearInfo]):
        if self.config_.reference_ is None:
            self.ref_context_ = None
            return

        ref_adapter_type = self.config_.reference_.type_
        self.ref_context_ = INFERENCECONTEXT_CLASS[ref_adapter_type](
            self.config_.reference_, linears_info
        )

    def ppo_pre_context(self, linears_info: OrderedDict[str, LinearInfo]):
        reward_adapter_type_ = self.config_.reward_adapter_.type_
        critic_adapter_type_ = self.config_.critic_adapter_.type_
        actor_adapter_type_ = self.config_.actor_adapter_.type_

        assert reward_adapter_type_ in TRAINCONTEXT_CLASS
        assert critic_adapter_type_ in TRAINCONTEXT_CLASS
        assert actor_adapter_type_ in TRAINCONTEXT_CLASS

        self.reward_context_ = TRAINCONTEXT_CLASS[reward_adapter_type_](
            self.config_.reward_adapter_, linears_info
        )
        self.critic_context_ = TRAINCONTEXT_CLASS[critic_adapter_type_](
            self.config_.critic_adapter_, linears_info
        )
        self.actor_context_= TRAINCONTEXT_CLASS[actor_adapter_type_](
            self.config_.actor_adapter_, linears_info
        )
        self._pre_ref_context(linears_info)

    def ppo_mse(self, data: torch.Tensor,label: torch.Tensor) -> torch.Tensor:
        return  (data - label).pow(2).mean()


    def ppo_adv_loss(self, prob: torch.Tensor, old_prob: torch.Tensor,
                     adv: torch.Tensor, a: torch.Tensor,)-> torch.Tensor:
        entropy = Categorical(prob.view(-1,prob.shape[-1])).entropy().mean(dim=-1)
        prob_a = prob.gather(-1, a).squeeze(-1)
        old_prob_a=old_prob.gather(-1,a).squeeze(-1)
        ratio = torch.exp(torch.log(prob_a) - torch.log(old_prob_a))
        # a/b == exp(log(a)-log(b))

        surr1 = ratio * adv
        surr2 = torch.clamp(ratio, 1 - self.config_.clip_rate_, 1 + self.config_.clip_rate_) * adv
        loss1 = -torch.min(surr1, surr2) - self.config_.entropy_coef_ * entropy
        loss1 = loss1.view(-1).mean()

        return loss1
    
    def ppo_reward_loss(self, reward_chosen: torch.Tensor, reward_reject: torch.Tensor)-> torch.Tensor:
        return reward_chosen-reward_reject

    @override
    def adapter_model(self) -> List[AdapterModel]:

        sq=[self.reward_context_.adapter_model(),self.actor_context_.adapter_model(), 
               self.critic_context_.adapter_model()]
        if self.ref_context_ is not None:
            sq.append(self.ref_context_.adapter_model())
        
        return sq

    @override
    def adapter_name(self) -> list[str]:

        sq=[self.config_.reward_adapter_.name_,self.config_.actor_adapter_.name_,
           self.config_.critic_adapter_.name_]
        if self.config_.reference_ is not None:
            sq.append(self.config_.reference_.name_)

        return sq

    @override
    def switch_device(self, device: str):
        if self.ref_context_ is not None:
            self.ref_context_.switch_device(device)
        self.critic_context_.switch_device(device)
        self.actor_context_.switch_device(device)
        self.reward_context_.switch_device(device)
        self.adv=self.adv.to(device)
        self.td_target=self.td_target.to(device)
        self.perm=self.perm.to(device)

    def stage_0(self, start_idx: int):        

        data_idx_s = self.now_data_idx_
        data_idx_e = self.now_data_idx_ + self.config_.mini_batch_size_
        # get the train raw string
        batch_strr = self.prompter_.generate_prompt(self.data_[data_idx_s:data_idx_e])
        reward_tokens = list(
            map(
                lambda raw_str: self.tokenizer_.encode(
                    raw_str, bos=True, eos=True, cutoff_len=self.config_.cutoff_len_
                ),
                batch_strr,
            )
        )

        l=int(len(reward_tokens)/3)
        reward_tokens=reward_tokens[l:]
        reward_start_idx=start_idx
        reward_end_idx=reward_start_idx+len(reward_tokens)
        
        def loss_fn(input: torch.Tensor, _: torch.Tensor, mask: torch.Tensor):
            if(PPOTask.reward_tensor is None):
                self.init_tensor(input.shape[-1])
            
            mask = ~mask[reward_start_idx:reward_end_idx]
            reward=input@PPOTask.reward_tensor
            reward=torch.sigmoid(reward).squeeze(dim=-1)
            l=int(len(reward)/2)
            mask=mask.to(self.td_target.device)
            reward_chosen=reward[:l]*mask[:l]
            reward_reject=reward[l:]*mask[l:]
            loss=torch.mean((reward_chosen-reward_reject))

            return loss

        reward_data_config = MLoRADataConfig(
            self.reward_context_.name_,
            self.reward_context_.type_,
            reward_start_idx,
            reward_end_idx,
            self._expand_batch_tokens,
            loss_fn,
            self.task_name(),
        )

        return reward_tokens,[reward_data_config]

    def stage_1(self, start_idx:int):
        logging.info(f"reward_model's ready")

        data_idx_s = self.now_data_idx_
        data_idx_e = self.now_data_idx_ + self.config_.mini_batch_size_

        # get the train raw string
        batch_str = self.prompter_.generate_prompt(self.data_[data_idx_s:data_idx_e])
        actor_tokens = list(
                map(
                    lambda raw_str: self.tokenizer_.encode(
                        raw_str, bos=True, eos=True, cutoff_len=self.config_.cutoff_len_
                    ),
                    batch_str,
                )
            )

        l=int(len(actor_tokens)/3)
        actor_tokens=actor_tokens[:l]
        batch_num=int(len(actor_tokens))
        generate_num=self.config_.generate_num_
        BOS=actor_tokens[0][0]
        EOS=actor_tokens[0][-1]
        critic_tokens=[[BOS]+[0]*generate_num+[EOS] for i in range(batch_num)]

        self.policy_tokens=[]
        self.policy_tokens.extend(actor_tokens)
        self.policy_tokens.extend(critic_tokens)

        self.state_=Stage.policy_training_decision

    def stage_2(self, input: torch.Tensor, actor_start_idx: int, actor_end_idx: int, 
               deterministic: bool = False):
        critic_len=int(len(self.policy_tokens[-1]))
        if(self.idx==critic_len-1): 
            self.state_=Stage.policy_training_update
        if(self.state_!=Stage.policy_training_decision):
            return

        batch_num=int(len(self.policy_tokens)/2)
        actor_len=int(len(self.policy_tokens[0]))

        idx=self.idx
        with torch.no_grad():
            if deterministic:
                a = torch.argmax(input[actor_start_idx:actor_end_idx,actor_len-1],dim=-1)
            else:
                input_=torch.softmax(input[actor_start_idx:actor_end_idx,actor_len-1].view(-1, input[actor_start_idx:actor_end_idx].shape[-1]),dim=-1)
                m = Categorical(input_)
                a = m.sample()
                a=a.view(batch_num,-1)
        
        for i in range(batch_num):
            self.policy_tokens[i].append(int(a[i].item()))
        for i in range(batch_num,2*batch_num):
            self.policy_tokens[i][idx]=int(a[i-batch_num].item())
        self.idx+=1

    def stage_3(self, start_idx:int):

        if self.state_==Stage.policy_training_init:
            self.stage_1(start_idx)
            self.state_=Stage.policy_training_decision

        batch_num=int(len(self.policy_tokens)/2) 
        ref_len=int(len(self.policy_tokens[0]))
        actor_len=int(len(self.policy_tokens[0])) # the real actor's len
        critic_len=int(len(self.policy_tokens[-1]))# the real critic's len
        reward_tokens = copy.deepcopy(self.policy_tokens[:batch_num])
        ref_tokens = copy.deepcopy(self.policy_tokens[:batch_num])
        p_tokens: List[List[int]] = []
        p_tokens.extend(reward_tokens)
        p_tokens.extend(ref_tokens)
        p_tokens.extend(self.policy_tokens)
       
        reward_start_idx=start_idx
        reward_end_idx=reward_start_idx+batch_num
        ref_start_idx=reward_end_idx
        ref_end_idx=ref_start_idx + batch_num
        actor_start_idx = ref_end_idx
        actor_end_idx = actor_start_idx + batch_num
        critic_start_idx = actor_end_idx
        critic_end_idx = critic_start_idx + batch_num

        def loss_fn(
                input: torch.Tensor, _: torch.Tensor, __: torch.Tensor
        )-> Optional[torch.Tensor]:
            
            if self.state_==Stage.policy_training_decision:
                self.stage_2(input,actor_start_idx,actor_end_idx)
            if self.state_==Stage.policy_training_decision:
                return None

            #Dividing a long trajectory into shorter trajectories for updating
            assert (self.config_.generate_num_)%self.config_.optim_num_==0
            data_len=int(self.config_.generate_num_/self.config_.optim_num_)

            p=input[actor_start_idx:actor_end_idx,actor_len-critic_len+1:actor_len-1].softmax(dim=-1)
            log_p=input[actor_start_idx:actor_end_idx,actor_len-critic_len+1:actor_len-1].log_softmax(dim=-1)
            log_ref_p=input[ref_start_idx:ref_end_idx,ref_len-critic_len+1:ref_len-1].log_softmax(dim=-1)
            action=torch.tensor(self.policy_tokens[batch_num:],device=self.adv.device)[:,1:-1].unsqueeze(dim=-1)
            log_prob = log_p.gather(-1, action).squeeze(-1)
            ref_log_prob = log_ref_p.gather(-1, action).squeeze(-1)
            r=-(log_prob-ref_log_prob)
            r[:,-1]+=torch.tanh(input[reward_start_idx:reward_end_idx,-1]@PPOTask.reward_tensor).squeeze(dim=-1)

            v=torch.tanh((input[critic_start_idx:critic_end_idx,1:critic_len]@PPOTask.critic_tensor).squeeze(dim=-1))
            v_=v.clone().detach()
            v_[:,-1]=0

            if(self.now_K_epochs==0 and self.now_optim_iter_num==0):
                deltas=torch.zeros_like(v_)
                for j in range(1,len(deltas[0])):
                    deltas[:,j-1]=r[:,j-1]+self.config_.gamma_*v_[:,j]-v_[:,j-1]

                adv=torch.zeros_like(v_)

                for j in range(len(adv[0])-2,-1,-1):
                    adv[:,j]=deltas[:,j]+self.config_.gamma_*self.config_.lamdb_*adv[:,j+1]
                
                adv=torch.flip(adv,[-1])
                adv = adv[:,0:-1]
                v_=v_[:,0:-1]
                td_target = adv + v_
                self.adv=adv
                self.td_target=td_target
                self.old_p=p

            if(self.now_optim_iter_num==0):
                self.perm = torch.randperm(len(self.adv[0])) 

            adv_ = self.adv[:,self.perm].clone().detach()
            td_target_ = self.td_target[:,self.perm].clone().detach()
            old_p = self.old_p[:,self.perm].clone().detach()
            p=torch.softmax(p,dim=-1)
            p = p[:,self.perm]
            v_=v_[:,self.perm]
            action=action.to(self.adv.device)
            action=action[:,self.perm]

            index=[i for i in range(self.now_optim_iter_num * data_len, min((self.now_optim_iter_num + 1) * data_len, len(self.adv[0])))]
            loss1=self.critic_context_.loss_fn_(v_[:,index].view(-1),td_target_[:,index].view(-1))
            loss2=self.actor_context_.loss_fn_(p[:,index],old_p[:,index],adv_[:,index],action[:,index])
            loss=loss1+loss2

            self.now_optim_iter_num+=1
            if(self.now_optim_iter_num==self.config_.optim_num_):
                self.now_K_epochs+=1
                self.now_optim_iter_num=0

            if(self.now_K_epochs==self.config_.K_epochs_): 
                self.now_K_epochs=0
                self.state_=Stage.policy_training_iteration

            logging.info(
                f"Adapter {self.critic_context_.name_} loss: {loss1} "
                f"Adapter {self.actor_context_.name_} loss: {loss2} "
                )

            return loss

        ref_model_name = ""
        ref_model_type = ""
        if self.ref_context_ is not None:
            ref_model_name = self.ref_context_.name_
            ref_model_type = self.ref_context_.type_

        reward_data_config = MLoRADataConfig(
            self.reward_context_.name_,
            self.reward_context_.type_,
            reward_start_idx,
            reward_end_idx,
            self._expand_batch_tokens,
            lambda *_: None,
            self.task_name()
        )
        ref_data_config = MLoRADataConfig(
            ref_model_name,
            ref_model_type,
            ref_start_idx,
            ref_end_idx,
            self._expand_batch_tokens,
            lambda *_: None,
            self.task_name(),
        )
        actor_data_config = MLoRADataConfig(
            self.actor_context_.name_,
            self.actor_context_.type_,
            actor_start_idx,
            actor_end_idx,
            self._expand_batch_tokens,
            lambda *_: None,
            self.task_name(),
        )
        critic_data_config = MLoRADataConfig(
            self.critic_context_.name_,
            self.critic_context_.type_,
            critic_start_idx,
            critic_end_idx,
            self._expand_batch_tokens,
            loss_fn,
            self.task_name(),
        )

        return p_tokens, [reward_data_config,ref_data_config,actor_data_config,critic_data_config]

    @override
    def data(self, start_idx: int) -> Tuple[List[Tokens], List[MLoRADataConfig]]:

        logging.info(
            f"Task - {self.reward_context_.name_,self.actor_context_.name_, self.critic_context_.name_} "
            f"epoch: {self.now_epoch_}/{self.config_.num_epochs_} "
            f"iteration: {self.now_data_idx_}/{len(self.data_)} step: {self.now_step_} "
            f"state: {self.state_} "
            f"idx: {self.idx} "
            f"now_K_epoch: {self.now_K_epochs} "
            f"now_optim_num: {self.now_optim_iter_num} "
        )

        stages=[self.stage_0,self.stage_3,self.stage_3,self.stage_3]

        return stages[self.state_.value](start_idx)

    @override
    def step(self):
        if self.state_==Stage.policy_training_init or self.state_==Stage.policy_training_decision:
            return

        stepd: bool = False
        need_checkpoint: bool = False
        is_reward_training: bool = (self.state_==Stage.reward_model_training)

        if self.now_step_ % self.config_.accumulate_step_ == 0:
            stepd = True
            if is_reward_training:
                self.reward_context_.step()
            else:
                self.critic_context_.step()
                self.actor_context_.step()

        if self.now_step_ % self.config_.save_step_ == 0:
            need_checkpoint = True

        self.now_step_ += 1

        if self.state_==Stage.reward_model_training:
            self.now_data_idx_ += self.config_.mini_batch_size_            

        if self.state_==Stage.policy_training_iteration: 
            self.now_data_idx_ += self.config_.mini_batch_size_
            self.state_=Stage.policy_training_init
            self.idx=1

        if self.now_data_idx_ >= len(self.data_):
            self.now_epoch_ += 1
            self.now_data_idx_ = 0

        # to save the checkpoint, must ensure the order
        # beacuse we need recover the state
        if need_checkpoint:
            self._save(is_checkpoint=True)

        # task finish we also need to step
        if not stepd and self.now_epoch_ == self.config_.num_epochs_:
            if is_reward_training:
                self.reward_context_.step()
            else:    
                self.actor_context_.step()
                self.critic_context_.step()

        if is_reward_training and self.now_epoch_ == self.config_.num_epochs_:
            self.state_=Stage.policy_training_init
            self.now_epoch_=0
            self.now_step_=1

    def __save(self, context_:TrainTaskContext, is_checkpoint: bool = False, 
    additional_info: Dict[str, str] = {}):
        output_dir = context_.path_
        if is_checkpoint:
            checkpoint_folder = "checkpoint_" + "_".join(
                [
                    str(self.now_step_),
                    str(self.now_epoch_),
                    str(self.now_data_idx_),
                ]
            )
            output_dir = context_.path_ + os.sep + checkpoint_folder

        if not os.path.exists(output_dir):
            os.makedirs(output_dir)

        # save to disk, if save checkpoint, we need also save the state dict
        if is_checkpoint:
            torch.save(
                {
                    "weight_dict": context_.weight_dict(),
                    "state_dict": context_.state_dict(),
                },
                output_dir + os.sep + "checkpoint.bin",
            )
            # Save checkpoint for shuffle_data.
            self._save_data(output_dir)

        else:
            torch.save(
                context_.weight_dict(), output_dir + os.sep + "adapter_model.bin"
            )

        adapter_config: Dict[str, str] = {}
        tmp_dict: Dict[str, str] = {}
        if "reward" in context_.name_:
            tmp_dict=self.config_.reward_adapter_.export()
        elif "actor" in context_.name_:
            tmp_dict=self.config_.actor_adapter_.export()
        else:
            tmp_dict=self.config_.critic_adapter_.export()
        adapter_config["base_model_name_or_path"] = self.llm_name_
        adapter_config = {**adapter_config, **additional_info}
        
        adapter_config = {**adapter_config, **tmp_dict}

        with open(output_dir + os.sep + "adapter_config.json", "w") as f:
            json.dump(adapter_config, f, indent=4)

    def _save(self, is_checkpoint: bool = False, additional_info: Dict[str, str] = {}):
       self.__save(self.actor_context_, is_checkpoint, additional_info)
       self.__save(self.critic_context_, is_checkpoint, additional_info)
       self.__save(self.reward_context_, is_checkpoint, additional_info)

    @override
    def done(self):
        self._save(is_checkpoint=False)
        # Delete the cache file.
        self._del_cache_file()
        # release the context
        del self.critic_context_
        del self.actor_context_
        del self.reward_context_
        if self.ref_context_ is not None:
            del self.ref_context_

    @override
    def terminate(self):
        del self.critic_context_
        del self.actor_context_
        del self.reward_context_
        if self.ref_context_ is not None:
            del self.ref_context_
        