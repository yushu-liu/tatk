"""
Sequicity is an end-to-end task-oriented dialog system based on a single sequence-to-sequence model that uses belief span to track dialog believes. We adapt the code from github to work in multiwoz corpus.

Reference:

Lei, W., Jin, X., Kan, M. Y., Ren, Z., He, X., & Yin, D. (2018, July). Sequicity: Simplifying task-oriented dialogue systems with single sequence-to-sequence architectures. In Proceedings of the 56th Annual Meeting of the Association for Computational Linguistics (Volume 1: Long Papers) (pp. 1437-1447).
"""
# -*- coding: utf-8 -*-
import os
import random
import zipfile
import json
import numpy as np
import torch
from nltk import word_tokenize
from torch.autograd import Variable

from tatk.util.file_util import cached_path
from tatk.e2e.sequicity.config import global_config as cfg
from tatk.e2e.sequicity.model import Model
from tatk.e2e.sequicity.reader import pad_sequences
from tatk.e2e.sequicity.tsd_net import cuda_
from tatk.dialog_agent import Agent

# DEFAULT_CUDA_DEVICE = -1
DEFAULT_DIRECTORY = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_CONFIG_FILE = os.path.join(DEFAULT_DIRECTORY, "multiwoz/configs/multiwoz.json")
DEFAULT_ARCHIVE_FILE_URL = "https://tatk-data.s3-ap-northeast-1.amazonaws.com/sequicity_multiwoz_data.zip"
DEFAULT_MODEL_URL = "https://tatk-data.s3-ap-northeast-1.amazonaws.com/sequicity_multiwoz.zip"


def denormalize(uttr):
    uttr = uttr.replace(' -s', 's')
    uttr = uttr.replace(' -ly', 'ly')
    uttr = uttr.replace(' -er', 'er')
    return uttr


class Sequicity(Agent):
    def __init__(self,
                 model_file=DEFAULT_MODEL_URL,
                 name='Sequicity'):
        """
        Sequicity initialization

        Args:
            model_file (str):
                trained model path or url. default="https://tatk-data.s3-ap-northeast-1.amazonaws.com/sequicity_multiwoz.zip"

        Example:
            sequicity = Sequicity()
        """
        super(Sequicity, self).__init__(name=name)
        config_file = DEFAULT_CONFIG_FILE
        c = json.load(open(config_file))
        cfg.init_handler(c['tsdf_init'])
        if not os.path.exists(os.path.join(DEFAULT_DIRECTORY,'multiwoz/data')):
            print('down load data from', DEFAULT_ARCHIVE_FILE_URL)
            archive_file = cached_path(DEFAULT_ARCHIVE_FILE_URL)
            archive = zipfile.ZipFile(archive_file, 'r')
            print('unzip to', os.path.join(DEFAULT_DIRECTORY,'multiwoz/'))
            archive.extractall(os.path.join(DEFAULT_DIRECTORY,'multiwoz/'))
            archive.close()
        model_path = os.path.join(DEFAULT_DIRECTORY,c['tsdf_init']['model_path'])
        if not os.path.exists(model_path):
            model_dir = os.path.dirname(model_path)
            if not os.path.exists(model_dir):
                os.makedirs(model_dir)
            print('Load from model_file param')
            print('down load data from', model_file)
            archive_file = cached_path(model_file)
            archive = zipfile.ZipFile(archive_file, 'r')
            print('unzip to', model_dir)
            archive.extractall(model_dir)
            archive.close()

        torch.manual_seed(cfg.seed)
        torch.cuda.manual_seed(cfg.seed)
        random.seed(cfg.seed)
        np.random.seed(cfg.seed)
        self.m = Model('multiwoz')
        self.m.count_params()
        self.m.load_model()
        self.init_session()

    def init_session(self):
        """Reset the class variables to prepare for a new session."""
        self.kw_ret = dict({'func': self.z2degree})

    def z2degree(self, gen_z):
        gen_bspan = self.m.reader.vocab.sentence_decode(gen_z, eos='EOS_Z2')
        constraint_request = gen_bspan.split()
        constraints = constraint_request[:constraint_request.index('EOS_Z1')] if 'EOS_Z1' \
                                                                                 in constraint_request else constraint_request
        for j, ent in enumerate(constraints):
            constraints[j] = ent.replace('_', ' ')
        degree = self.m.reader.db_search(constraints)
        degree_input_list = self.m.reader._degree_vec_mapping(len(degree))
        degree_input = cuda_(Variable(torch.Tensor(degree_input_list).unsqueeze(0)))
        return degree, degree_input

    def response(self, usr):
        """
        Generate agent response given user input.

        Args:
            observation (str):
                The input to the agent.
        Returns:
            response (str):
                The response generated by the agent.
        """
        # print('usr:', usr)
        usr = word_tokenize(usr.lower())
        usr_words = usr + ['EOS_U']
        u_len = np.array([len(usr_words)])
        usr_indices = self.m.reader.vocab.sentence_encode(usr_words)
        u_input_np = np.array(usr_indices)[:, np.newaxis]
        u_input = cuda_(Variable(torch.from_numpy(u_input_np).long()))
        m_idx, z_idx, degree = self.m.m(mode='test', degree_input=None, z_input=None,
                                        u_input=u_input, u_input_np=u_input_np, u_len=u_len,
                                        m_input=None, m_input_np=None, m_len=None,
                                        turn_states=None, **self.kw_ret)
        venue = random.sample(degree, 1)[0] if degree else dict()
        l = [self.m.reader.vocab.decode(_) for _ in m_idx[0]]
        if 'EOS_M' in l:
            l = l[:l.index('EOS_M')]
        l_origin = []
        for word in l:
            if 'SLOT' in word:
                word = word[:-5]
                if word in venue.keys():
                    value = venue[word]
                    if value != '?':
                        l_origin.append(value)
            elif word.endswith('reference]'):
                if 'ref' in venue:
                    l_origin.append(venue['ref'])
            else:
                l_origin.append(word)
        sys = ' '.join(l_origin)
        sys = denormalize(sys)
        # print('sys:', sys)
        if cfg.prev_z_method == 'separate':
            eob = self.m.reader.vocab.encode('EOS_Z2')
            if eob in z_idx[0] and z_idx[0].index(eob) != len(z_idx[0]) - 1:
                idx = z_idx[0].index(eob)
                z_idx[0] = z_idx[0][:idx + 1]
            for j, word in enumerate(z_idx[0]):
                if word >= cfg.vocab_size:
                    z_idx[0][j] = 2  # unk
            prev_z_input_np = pad_sequences(z_idx, cfg.max_ts, padding='post', truncating='pre').transpose((1, 0))
            prev_z_len = np.array([len(_) for _ in z_idx])
            prev_z_input = cuda_(Variable(torch.from_numpy(prev_z_input_np).long()))
            self.kw_ret['prev_z_len'] = prev_z_len
            self.kw_ret['prev_z_input'] = prev_z_input
            self.kw_ret['prev_z_input_np'] = prev_z_input_np
        return sys

if __name__ == '__main__':
    s = Sequicity()
    print(s.response("I want to find a cheap restaurant"))
    print(s.response("ok, what is the address ?"))
