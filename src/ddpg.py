#!/usr/bin/env python
# Authors: Oscar Wu #

import rospy
import os
import json
import numpy as np
import random
import time
import sys
import pickle
sys.path.append(os.path.dirname(os.path.abspath(os.path.dirname(__file__))))
from collections import deque
from std_msgs.msg import Float32
from environment_ddpg import Env
import torch
import torch.nn.functional as F
import gc
import torch.nn as nn
import math
from collections import deque
import copy
#torch.manual_seed(0)
#np.random.seed(0)
#random.seed(0)

dirPath = os.path.dirname(os.path.realpath(__file__))
#---Functions to make network updates---#
 
def soft_update(target, source, tau):
    for target_param, param in zip(target.parameters(), source.parameters()):
        target_param.data.copy_(target_param.data*(1.0 - tau)+ param.data*tau)

def hard_update(target,source):
    for target_param, param in zip(target.parameters(), source.parameters()):
        target_param.data.copy_(param.data)

#---Ornstein-Uhlenbeck Noise---#

class OUNoise(object):
    def __init__(self, action_space, mu=0.0, theta=0.15, max_sigma=0.99, min_sigma=0.01, decay_period= 600000):
        self.mu           = mu
        self.theta        = theta
        self.sigma        = max_sigma
        self.max_sigma    = max_sigma
        self.min_sigma    = min_sigma
        self.decay_period = decay_period
        self.action_dim   = action_space
        self.reset()
        
    def reset(self):
        self.state = np.ones(self.action_dim) * self.mu
        
    def evolve_state(self):
        x  = self.state
        dx = self.theta * (self.mu - x) + self.sigma * np.random.randn(self.action_dim)
        self.state = x + dx
        return self.state
    
    def get_noise(self, t=0): 
        ou_state = self.evolve_state()
        decaying = float(float(t)/ self.decay_period)
        self.sigma = max(self.sigma - (self.max_sigma - self.min_sigma) * min(1.0, decaying), self.min_sigma)
        return ou_state


#---Critic--#

EPS = 0.003
def fanin_init(size, fanin=None):
    fanin = fanin or size[0]
    v = 1./np.sqrt(fanin)
    return torch.Tensor(size).uniform_(-v,v)

class Critic(nn.Module):
    def __init__(self, state_dim, action_dim):
        super(Critic, self).__init__()
        
        self.state_dim = state_dim = state_dim
        self.action_dim = action_dim
        
        self.fc1 = nn.Linear(state_dim, 125)
        nn.init.xavier_uniform_(self.fc1.weight)
        self.fc1.bias.data.fill_(0.01)
        
        self.fa1 = nn.Linear(action_dim, 125)
        nn.init.xavier_uniform_(self.fa1.weight)
        self.fa1.bias.data.fill_(0.01)
        
        self.fca1 = nn.Linear(250, 250)
        nn.init.xavier_uniform_(self.fca1.weight)
        self.fca1.bias.data.fill_(0.01)
        
        self.fca2 = nn.Linear(250, 1)
        nn.init.xavier_uniform_(self.fca2.weight)
        self.fca2.bias.data.fill_(0.01)
        
    def forward(self, state, action):
        xs = torch.relu(self.fc1(state))
        xa = torch.relu(self.fa1(action))
        x = torch.cat((xs,xa), dim=1)
        x = torch.relu(self.fca1(x))
        vs = self.fca2(x)
        return vs

#---Actor---#

class Actor(nn.Module):
    def __init__(self, state_dim, action_dim, action_limit_v, action_limit_w):
        super(Actor, self).__init__()
        self.state_dim = state_dim = state_dim
        self.action_dim = action_dim
        self.action_limit_v = action_limit_v
        self.action_limit_w = action_limit_w
        
        self.fa1 = nn.Linear(state_dim, 250)
        nn.init.xavier_uniform_(self.fa1.weight)
        self.fa1.bias.data.fill_(0.01)
        
        self.fa2 = nn.Linear(250, 250)
        nn.init.xavier_uniform_(self.fa2.weight)
        self.fa2.bias.data.fill_(0.01)
        
        self.fa3 = nn.Linear(250, action_dim)
        nn.init.xavier_uniform_(self.fa3.weight)
        self.fa3.bias.data.fill_(0.01)
        
    def forward(self, state):
        x = torch.relu(self.fa1(state))
        x = torch.relu(self.fa2(x))
        action = self.fa3(x)
        if state.shape <= torch.Size([self.state_dim]):
            action[0] = torch.sigmoid(action[0])*self.action_limit_v
            action[1] = torch.tanh(action[1])*self.action_limit_w
        else:
            action[:,0] = torch.sigmoid(action[:,0])*self.action_limit_v
            action[:,1] = torch.tanh(action[:,1])*self.action_limit_w
        return action

#---Memory Buffer---#

class MemoryBuffer:
    def __init__(self, size):
        self.buffer = deque(maxlen=size)
        self.maxSize = size
        self.len = 0
        
    def sample(self, count):
        batch = []
        count = min(count, self.len)
        batch = random.sample(self.buffer, count)
        
        s_array = np.float32([array[0] for array in batch])
        a_array = np.float32([array[1] for array in batch])
        r_array = np.float32([array[2] for array in batch])
        new_s_array = np.float32([array[3] for array in batch])
        done_array = np.float32([array[4] for array in batch])
        
        return s_array, a_array, r_array, new_s_array, done_array
    
    def len(self):
        return self.len
    
    def add(self, s, a, r, new_s, done):
        transition = (s, a, r, new_s, done)
        self.len += 1 
        if self.len > self.maxSize:
            self.len = self.maxSize
        self.buffer.append(transition)

#---Train---#

BATCH_SIZE = 256
LEARNING_RATE = 0.001
GAMMA = 0.99
TAU = 0.001

class Trainer:
    
    def __init__(self, state_dim, action_dim, action_limit_v, action_limit_w, ram):
        
        self.state_dim = state_dim
        self.action_dim = action_dim
        self.action_limit_v = action_limit_v
        self.action_limit_w = action_limit_w
        self.ram = ram
        
        self.actor = Actor(self.state_dim, self.action_dim, self.action_limit_v, self.action_limit_w)
        self.target_actor = Actor(self.state_dim, self.action_dim, self.action_limit_v, self.action_limit_w)
        self.actor_optimizer = torch.optim.Adam(self.actor.parameters(), LEARNING_RATE)
        
        self.critic = Critic(self.state_dim, self.action_dim)
        self.target_critic = Critic(self.state_dim, self.action_dim)
        self.critic_optimizer = torch.optim.Adam(self.critic.parameters(), LEARNING_RATE)
        self.pub_qvalue = rospy.Publisher('qvalue', Float32, queue_size=5)
        self.qvalue = Float32()
        
        hard_update(self.target_actor, self.actor)
        hard_update(self.target_critic, self.critic)
        
    def get_exploitation_action(self,state):
        state = torch.from_numpy(state)
        action = self.actor.forward(state).detach()
        #print('actionploi', action)
        return action.data.numpy()
        
    def get_exploration_action(self, state):
        state = torch.from_numpy(state)
        action = self.actor.forward(state).detach()
        new_action = action.data.numpy()
        return new_action
    
    def optimizer(self):
        s_sample, a_sample, r_sample, new_s_sample, done_sample = ram.sample(BATCH_SIZE)
        
        s_sample = torch.from_numpy(s_sample)
        a_sample = torch.from_numpy(a_sample)
        r_sample = torch.from_numpy(r_sample)
        new_s_sample = torch.from_numpy(new_s_sample)
        done_sample = torch.from_numpy(done_sample)
        
        #-------------- optimize critic
        
        a_target = self.target_actor.forward(new_s_sample).detach()
        next_value = torch.squeeze(self.target_critic.forward(new_s_sample, a_target).detach())
        # y_exp = r _ gamma*Q'(s', P'(s'))
        y_expected = r_sample + (1 - done_sample)*GAMMA*next_value
        # y_pred = Q(s,a)
        y_predicted = torch.squeeze(self.critic.forward(s_sample, a_sample))
        #-------Publisher of Vs------
        self.qvalue = y_predicted.detach()
        self.pub_qvalue.publish(torch.max(self.qvalue))
        loss_critic = F.smooth_l1_loss(y_predicted, y_expected)

        
        self.critic_optimizer.zero_grad()
        loss_critic.backward()
        self.critic_optimizer.step()
        
        #------------ optimize actor
        pred_a_sample = self.actor.forward(s_sample)
        loss_actor = -1*torch.sum(self.critic.forward(s_sample, pred_a_sample))
        
        self.actor_optimizer.zero_grad()
        loss_actor.backward()
        self.actor_optimizer.step()
        
        soft_update(self.target_actor, self.actor, TAU)
        soft_update(self.target_critic, self.critic, TAU)
    
    def save_models(self, episode_count):
        torch.save(self.target_actor.state_dict(), dirPath +'/Models/ddpg/' + str(episode_count)+ '_actor.pt')
        torch.save(self.target_critic.state_dict(), dirPath + '/Models/ddpg/' + '/'+str(episode_count)+ '_critic.pt')
        
    def load_models(self, episode):
        self.actor.load_state_dict(torch.load(dirPath + '/Models/ddpg/' + str(episode)+ '_actor.pt'))
        self.critic.load_state_dict(torch.load(dirPath + '/Models/ddpg/' + str(episode)+ '_critic.pt'))
        hard_update(self.target_actor, self.actor)
        hard_update(self.target_critic, self.critic)

#---Run agent---#

is_training = True

exploration_decay_rate = 0.001

MAX_EPISODES = 10001
MAX_STEPS = 500
MAX_BUFFER = 200000
rewards_all_episodes = []
Qvalue_all = []

STATE_DIMENSION = 14
ACTION_DIMENSION = 2
ACTION_V_MAX = 0.22 # m/s
ACTION_W_MAX = 2. # rad/s


if is_training:
    var_v = ACTION_V_MAX*.5
    var_w = ACTION_W_MAX*2*.5
else:
    var_v = ACTION_V_MAX*0.10
    var_w = ACTION_W_MAX*0.10

print('State Dimensions: ' + str(STATE_DIMENSION))
print('Action Dimensions: ' + str(ACTION_DIMENSION))
print('Action Max: ' + str(ACTION_V_MAX) + ' m/s and ' + str(ACTION_W_MAX) + ' rad/s')
ram = MemoryBuffer(MAX_BUFFER)
trainer = Trainer(STATE_DIMENSION, ACTION_DIMENSION, ACTION_V_MAX, ACTION_W_MAX, ram)
noise = OUNoise(ACTION_DIMENSION, max_sigma=0.1, min_sigma=0.1, decay_period=8000000)
start = 1
#trainer.load_models(start)


if __name__ == '__main__':
    rospy.init_node('ddpg_stage_1')
    pub_result = rospy.Publisher('result', Float32, queue_size=5)
    result = Float32()
    env = Env(action_dim=ACTION_DIMENSION)
    before_training = 5
    Q_flag = False

    past_action = np.zeros(ACTION_DIMENSION)

    for ep in range(start,MAX_EPISODES):
        done = False
        state = env.reset()
        if is_training and not ep%10 == 0 and ram.len >= before_training*MAX_STEPS:
            print('---------------------------------')
            print('Episode: ' + str(ep) + ' training')
            print('---------------------------------')
        else:
            if ram.len >= before_training*MAX_STEPS:
                print('---------------------------------')
                print('Episode: ' + str(ep) + ' evaluating')
                print('---------------------------------')
            else:
                print('---------------------------------')
                print('Episode: ' + str(ep) + ' adding to memory')
                print('---------------------------------')

        rewards_current_episode = 0.

        for step in range(MAX_STEPS):
            state = np.float32(state)

            if is_training and not ep%10 == 0:
                action = trainer.get_exploration_action(state)
                N = copy.deepcopy(noise.get_noise(t=step))
                N[0] = N[0]*ACTION_V_MAX/2
                N[1] = N[1]*ACTION_W_MAX
                action[0] = np.clip(action[0] + N[0], 0., ACTION_V_MAX)
                action[1] = np.clip(action[1] + N[1], -ACTION_W_MAX, ACTION_W_MAX)
            else:
                action = trainer.get_exploration_action(state)

            if not is_training:
                action = trainer.get_exploitation_action(state)
            next_state, reward, done = env.step(action, past_action)
            past_action = copy.deepcopy(action)
            
            rewards_current_episode += reward
            next_state = np.float32(next_state)
            if not ep%10 == 0 or not ram.len >= before_training*MAX_STEPS:
                if reward == 100.:
                    print('-------- Arrived ----------')
                    for _ in range(3):
                        ram.add(state, action, reward, next_state, done)

                else:
                    ram.add(state, action, reward, next_state, done)
            state = copy.deepcopy(next_state)
            

            if ram.len >= before_training*MAX_STEPS and is_training and not ep%10 == 0:
                trainer.optimizer()
                Q_flag = True 

            if done or step == MAX_STEPS-1:
                print('reward per ep: ' + str(rewards_current_episode))
                print('*\nbreak step: ' + str(step) + '\n*')
                print('sigma: ' + str(noise.sigma))
                rewards_all_episodes.append(rewards_current_episode)

                # Q_flag represent optimizer start working
                if Q_flag:                
                        Qvalue_all.append(torch.max(trainer.qvalue.data).item())
                else:
                        Qvalue_all.append(trainer.qvalue.data)
                if not ep%10 == 0:
                    pass
                else:
                    result = rewards_current_episode
                    pub_result.publish(result)
                break
        if ep%20 == 0:
            trainer.save_models(ep)
        with open(dirPath + '/Models/ddpg/' + 'DDPG_Reward.txt','wb') as f:
                pickle.dump(rewards_all_episodes,f)
        with open(dirPath + '/Models/ddpg/' + 'DDPG_Qvalue.txt','wb') as f:
                pickle.dump(Qvalue_all,f)
