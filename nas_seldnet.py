import argparse
import copy
import joblib
import json
import time
import tensorflow as tf
import tensorflow.keras.backend as K

import layers
import model_complexity
import models
import stage_complexity
from config_sampler import conv_temporal_sampler
from data_loader import *
from metrics import *
from transforms import *
from utils import dict_add

args = argparse.ArgumentParser()

args.add_argument('--name', type=str, required=True,
                  help='name must be {name}_{divided index} ex) 2021_1')
args.add_argument('--dataset_path', type=str, 
                  default='/root/datasets/DCASE2021/feat_label')
args.add_argument('--n_samples', type=int, default=250)
args.add_argument('--n_blocks', type=int, default=3)
args.add_argument('--min_flops', type=int, default=200_000_000)
args.add_argument('--max_flops', type=int, default=240_000_000)

args.add_argument('--batch_size', type=int, default=256)
args.add_argument('--n_repeat', type=int, default=50)
args.add_argument('--lr', type=int, default=1e-3)
args.add_argument('--n_classes', type=int, default=12)
args.add_argument('--gpus', type=str, default='-1')


'''            SEARCH SPACES           '''
search_space_2d = {
    'mother_stage':
        {'depth': [1],
        # {'depth': [1, 2, 3],
         'filters0': [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 
                      3, 4, 6, 8, 12, 16, 24, 32, 48, 64, 96, 128, 192, 256],
        #  'filters0': [0],
         'filters1': [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 
                      3, 4, 6, 8, 12, 16, 24, 32, 48, 64, 96, 128, 192, 256],
        #  'filters1': [3, 4, 6, 8, 12, 16, 24, 32, 48, 64, 96, 128, 192, 256],
        #  'filters2': [0],
         'filters2': [3, 4, 6, 8, 12, 16, 24, 32, 48, 64, 96, 128, 192, 256],
        #  'filters2': [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 
        #               3, 4, 6, 8, 12, 16, 24, 32, 48, 64, 96, 128, 192, 256],
         'kernel_size0': [1, 3, 5],
         'kernel_size1': [1, 3, 5],
         'kernel_size2': [1, 3, 5],
         'connect0': [[0], [1]],
         'connect1': [[0, 0], [0, 1], [1, 0], [1, 1]],
        #  'connect2': [[0, 0, 0], [0, 0, 1], [0, 1, 0], [0, 1, 1],
        #               [1, 0, 0], [1, 0, 1], [1, 1, 0], [1, 1, 1]],
         'connect2': [[0, 0, 0], [0, 1, 0], [0, 1, 1],
                      [1, 0, 0], [1, 0, 1], [1, 1, 0], [1, 1, 1]],
        #  'strides': [(1, 1), (1, 2), (1, 3)]},
         'strides': [(1, 2)]},
}
search_space_1d = {
    'bidirectional_GRU_stage':
        {'depth': [1, 2, 3],
         'units': [16, 24, 32, 48, 64, 96, 128, 192, 256]}, 
    'transformer_encoder_stage':
        {'depth': [1],
        # {'depth': [1, 2, 3],
         'n_head': [1, 2, 4, 8, 16],
         'key_dim': [2, 3, 4, 6, 8, 12, 16, 24, 32, 48],
         'ff_multiplier': [0.25, 0.5, 1, 2, 4, 8],
         'kernel_size': [1, 3]},
        #  'kernel_size': [1, 3, 5]},
    # 'simple_dense_stage':
    #     {'depth': [1, 2, 3],
    #      'units': [4, 6, 8, 12, 16, 24, 32, 48, 64, 96, 128, 192, 256],
    #      'dense_activation': ['relu'],
    #      'dropout_rate': [0., 0.2, 0.5]},
    'conformer_encoder_stage':
        {'depth': [1, 2],
         'key_dim': [2, 3, 4, 6, 8, 12, 16, 24, 32, 48],
         'n_head': [1, 2, 4, 8, 16],
         'kernel_size': [4, 6, 8, 12, 16, 24, 32, 48, 64, 96, 128, 192, 256],
         'multiplier': [1, 2, 4],
         'pos_encoding': ['basic']},
        #  'pos_encoding': [None, 'basic', 'rff']},
}


def sample_constraint(min_flops=None, max_flops=None, 
                      min_params=None, max_params=None):
    def _contraint(model_config, input_shape):
        def get_complexity(block_type):
            return getattr(stage_complexity, f'{block_type}_complexity')

        shape = input_shape
        total_cx = {}

        blocks = sorted([b for b in model_config.keys()
                         if b.startswith('BLOCK') and not b.endswith('_ARGS')])

        try:
            for block in blocks:
                cx, shape = get_complexity(model_config[block])(
                    model_config[f'{block}_ARGS'], shape)
                total_cx = dict_add(total_cx, cx)

                if model_config[block] == 'mother_stage':
                    args = model_config[f'{block}_ARGS']
                    n_convs = ((args['filters0'] > 0)
                               + (args['filters1'] > 0)
                               + (args['filters2'] > 0))

                    if n_convs == 1:
                        if args['filters1'] == 0:
                            return False
                    elif n_convs == 2:
                        if args['filters1'] > 0 \
                                and list(args['strides']) == [1, 1]:
                            return False
                    # else:
                    #     return False
            
            # 1차
            if model_config['DOA'] != 'bidirectional_GRU_stage': # DOA GRU 고정
                return False

            # identity = 0 # identity stage가 무조건 1인 경우만
            # for block in blocks:
            #     if model_config[block] == 'mother_stage':
            #         args = model_config[f'{block}_ARGS']
            #         n_convs = ((args['filters0'] > 0)
            #                    + (args['filters1'] > 0)
            #                    + (args['filters2'] > 0))
            #         if n_convs == 0:
            #             identity += 1
            # if identity != 1:
            #     return False

            # 2차
            num_1d = 0 # 1d stage가 최소 1개이상 존재
            for block in blocks:
                if model_config[block] != 'mother_stage':
                    num_1d += 1
            if num_1d in [4]:
                return False

            # if model_config['SED'] == 'bidirectional_GRU_stage': # SED GRU 제거
            #     return False

            # if model_config['BLOCK2'] == 'bidirectional_GRU_stage':
            #     return False

            # if model_config['BLOCK3'] == 'bidirectional_GRU_stage':
            #     return False

            # 3차
            # constraint 조정 없음

            # 4차
            if model_config['BLOCK1'] != 'bidirectional_GRU_stage':
                return False

            if model_config['BLOCK0'] != 'mother_stage':
                return False

            if model_config['BLOCK0_ARGS']['depth'] == 3:
                return False

            # 5차
            if model_config['BLOCK2'] != 'transformer_encoder_stage': # block2 transformer 고정
                return False

            if not model_config['DOA_ARGS']['depth'] in [1, 3]:
                return False
                
            # 6차
            if model_config['SED'] != 'conformer_encoder_stage': # SED conformer 고정
                return False

            if model_config['BLOCK1_ARGS']['units'] == 16:
                return False

            if model_config['BLOCK1_ARGS']['depth'] != 1:
                return False

            # 7차
            # 직접 서치 스페이스에서 줄임

            # 8차
            if model_config['BLOCK1_ARGS']['units'] in [8, 24]:
                return False

            # 9차
            if model_config['DOA_ARGS']['depth'] != 3:
                return False
            
            
            

            cx, sed_shape = get_complexity(model_config['SED'])(
                model_config['SED_ARGS'], shape)
            cx, sed_shape = stage_complexity.linear_complexity(
                sed_shape, model_config['n_classes'], prev_cx=cx)
            total_cx = dict_add(total_cx, cx)

            cx, doa_shape = get_complexity(model_config['DOA'])(
                model_config['DOA_ARGS'], shape)
            cx, doa_shape = stage_complexity.linear_complexity(
                doa_shape, 3*model_config['n_classes'], prev_cx=cx)
            total_cx = dict_add(total_cx, cx)

        except ValueError as e:
            return False

        # total complexity contraint
        if min_flops and total_cx['flops'] < min_flops:
            return False
        if max_flops and total_cx['flops'] > max_flops:
            return False
        if min_params and total_cx['params'] < min_params:
            return False
        if max_params and total_cx['params'] > max_params:
            return False
        return True
    return _contraint


def postprocess_fn(model_config):
    model_config = copy.deepcopy(model_config)
    blocks = sorted([key for key in model_config.keys()
                     if key.startswith('BLOCK') and not key.endswith('_ARGS')])

    for block in blocks:
        stage_type = model_config[block]
        
        if stage_type == 'mother_stage':
            args = model_config[f'{block}_ARGS']
            if args['filters2'] == 0:
                if args['filters1'] != 0:
                    args['connect2'][2] = 1
                elif args['filters0'] != 0:
                    args['connect2'][1] = 1

            if args['filters0'] == 0:
                args['kernel_size0'] = 0
                args['connect1'][1] = 0
                args['connect2'][1] = 0
            if args['filters1'] == 0:
                args['kernel_size1'] = 0
                args['connect2'][2] = 0
                args['strides'] = [1, 1]
            if args['filters2'] == 0:
                args['kernel_size2'] = 0
    return model_config


def train_and_eval(train_config,
                   model_config: dict,
                   input_shape,
                   trainset: tf.data.Dataset,
                   valset: tf.data.Dataset,
                   evaluator):
    model = models.conv_temporal(input_shape, model_config)
    optimizer = tf.keras.optimizers.Adam(train_config.lr)

    model.compile(optimizer=optimizer,
                  loss={'sed_out': tf.keras.losses.BinaryCrossentropy(),
                        'doa_out': tf.keras.losses.MSE},
                  loss_weights=[1, 1000])

    history = model.fit(trainset,
                        validation_data=valset)

    evaluator.reset_states()
    for x, y in valset:
        evaluator.update_states(y, model(x, training=False))
    scores = evaluator.result()
    scores = {
        'val_error_rate': scores[0].numpy().tolist(),
        'val_f1score': scores[1].numpy().tolist(),
        'val_der': scores[2].numpy().tolist(),
        'val_derf': scores[3].numpy().tolist(),
        'val_seld_score': calculate_seld_score(scores).numpy().tolist(),
    }

    performances = {
        **history.history,
        **scores,
        **(model_complexity.conv_temporal_complexity(model_config, 
                                                     input_shape)[0])
    }
    del model, optimizer, history
    return performances


# reference: https://github.com/IRIS-AUDIO/SELD.git
def random_ups_and_downs(x, y):
    stddev = 0.25
    offsets = tf.linspace(tf.random.normal([], stddev=stddev),
                          tf.random.normal([], stddev=stddev),
                          x.shape[-3])
    offsets_shape = [1] * len(x.shape)
    offsets_shape[-3] = offsets.shape[0]
    offsets = tf.reshape(offsets, offsets_shape)
    x = tf.concat([x[..., :4] + offsets, x[..., 4:]], axis=-1)
    return x, y


def get_dataset(config, mode: str = 'train'):
    path = config.dataset_path
    x, y = load_seldnet_data(os.path.join(path, 'foa_dev_norm'),
                             os.path.join(path, 'foa_dev_label'),
                             mode=mode, n_freq_bins=64)
    if mode == 'train':
        sample_transforms = [
            random_ups_and_downs,
            lambda x, y: (mask(x, axis=-2, max_mask_size=16), y),
        ]
        batch_transforms = [foa_intensity_vec_aug]
    else:
        sample_transforms = []
        batch_transforms = []
    batch_transforms.append(split_total_labels_to_sed_doa)

    dataset = seldnet_data_to_dataloader(
        x, y,
        train= mode == 'train',
        batch_transforms=batch_transforms,
        label_window_size=60,
        batch_size=config.batch_size,
        sample_transforms=sample_transforms,
        loop_time=config.n_repeat
    )

    return dataset


if __name__=='__main__':
    train_config = args.parse_args()
    os.environ['CUDA_VISIBLE_DEVICES'] = train_config.gpus
    del train_config.gpus
    gpus = tf.config.experimental.list_physical_devices('GPU')
    print(gpus)
    if gpus:
        try:
            tf.config.experimental.set_virtual_device_configuration(
                gpus[0],
                [tf.config.experimental.VirtualDeviceConfiguration(
                    memory_limit=10240)])
        except RuntimeError as e:
            print(e)

    name = train_config.name
    if not name.endswith('.json'):
        name = f'{name}.json'

    input_shape = [300, 64, 7]

    # datasets
    trainset = get_dataset(train_config, mode='train')
    valset = get_dataset(train_config, mode='val')

    # Evaluator
    evaluator = SELDMetrics(doa_threshold=20, n_classes=train_config.n_classes)

    default_config = {
        'n_classes': train_config.n_classes
    }
    constraint = sample_constraint(train_config.min_flops, 
                                   train_config.max_flops)
    results = {'train_config': vars(train_config)}
    start_idx = 0

    # resume past results
    if os.path.exists(name):
        with open(name, 'r') as f:
            prev_results = json.load(f)

        if results['train_config'] != prev_results['train_config']:
            raise ValueError('prev config has different train_config')
        
        results = prev_results
        start_idx = 1 + max([int(k) for k in results.keys() if k.isdigit()])

    # start training
    for i in range(start_idx, train_config.n_samples):
        model_config = conv_temporal_sampler(
            search_space_2d,
            search_space_1d,
            n_blocks=train_config.n_blocks,
            input_shape=input_shape,
            default_config=default_config,
            config_postprocess_fn=postprocess_fn,
            constraint=constraint)

        start = time.time()
        outputs = train_and_eval(
            train_config, model_config, 
            input_shape, 
            trainset, valset, evaluator)
        outputs['time'] = time.time() - start

        results[f'{i:03d}'] = {'config': model_config, 'perf': outputs}
        with open(name, 'w') as f:
            json.dump(results, f, indent=4)

