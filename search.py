import argparse
import json
import os
import time

import tensorflow as tf

from data_loader import *
from metrics import *
from transforms import *
from config_sampler import get_config
from search_utils import postprocess_fn
from model_flop import get_flops
from model_size import get_model_size
from model_analyze import analyzer, narrow_search_space
from writer_manager import Writer
import models


args = argparse.ArgumentParser()

args.add_argument('--name', type=str, required=True,
                  help='name must be {name}_{divided index} ex) 2021_1')
args.add_argument('--dataset_path', type=str, 
                  default='/root/datasets/DCASE2021/feat_label')
args.add_argument('--n_samples', type=int, default=500)
args.add_argument('--min_samples', type=int, default=32)
args.add_argument('--verbose', action='store_true')
args.add_argument('--threshold', type=float, default=0.05)

args.add_argument('--batch_size', type=int, default=256)
args.add_argument('--n_repeat', type=int, default=5)
args.add_argument('--epoch', type=int, default=10)
args.add_argument('--lr', type=int, default=1e-3)
args.add_argument('--n_classes', type=int, default=12)
args.add_argument('--gpus', type=str, default='-1')
args.add_argument('--config', action='store_true', help='if true, reuse config')
args.add_argument('--new', action='store_true')

input_shape = [300, 64, 7]


'''            SEARCH SPACES           '''
block_2d_num = [1, 2]
block_1d_num = [0, 1, 2]
search_space_2d = {
    'mother_stage':
        {'mother_depth': [1, 2, 3],
        'filters0': [0, 3, 4, 6, 8, 12, 16, 24, 32, 48, 64],
        'filters1': [3, 4, 6, 8, 12, 16, 24, 32, 48, 64],
        'filters2': [0, 3, 4, 6, 8, 12, 16, 24, 32, 48, 64],
        'kernel_size0': [1, 3, 5],
        'kernel_size1': [1, 3, 5],
        'kernel_size2': [1, 3, 5],
        'connect0': [[0], [1]],
        'connect1': [[0, 0], [0, 1], [1, 0], [1, 1]],
        'connect2': [[0, 0, 0], [0, 0, 1], [0, 1, 0], [0, 1, 1],
                    [1, 0, 0], [1, 0, 1], [1, 1, 0], [1, 1, 1]],
        'strides': [(1, 1), (1, 2), (1, 3)]},
}
search_space_1d = {
    'bidirectional_GRU_stage':
        {'depth': [1, 2],
        'gru_units': [16, 24, 32, 48, 64, 96, 128]}, 
    'transformer_encoder_stage':
        {'depth': [1, 2],
        'n_head': [1, 2, 4, 8, 16],
        'key_dim': [2, 3, 4, 6, 8, 12, 16, 24, 32, 48],
        'ff_multiplier': [0.25, 0.5, 1, 2, 4, 8],
        'kernel_size': [1, 3, 5]},
    'simple_dense_stage':
        {'depth': [1, 2],
            'dense_units': [4, 6, 8, 12, 16, 24, 32, 48, 64, 96, 128],
            'dense_activation': ['relu'],
            'dropout_rate': [0., 0.2, 0.5]},
    'conformer_encoder_stage':
        {'depth': [1, 2],
        'key_dim': [2, 3, 4, 6, 8, 12, 16, 24, 32, 48],
        'n_head': [1, 2, 4, 8, 16],
        'kernel_size': [4, 6, 8, 12, 16, 24, 32, 48, 64],
        'multiplier': [1, 2, 4],
        'pos_encoding': [None, 'basic', 'rff']},
}


def train_and_eval(train_config,
                   model_config: dict,
                   input_shape,
                   trainset: tf.data.Dataset,
                   valset: tf.data.Dataset,
                   evaluator):
    try:
        model = models.conv_temporal(input_shape, model_config)
        model.summary()
    except:
        print('!!!!!!!!!!!!!!!model error occurs!!!!!!!!!!!!!!!')
        if not os.path.exists('error_models'):
            os.makedirs('error_models')
        configs = []
        if os.path.exists(os.path.join('error_models', 'error_model.json')):
            with open(os.path.join('error_models', 'error_model.json'), 'r') as f:
                configs = json.load(f)
        else:
            configs = [model_config]
        with open(os.path.join('error_models', 'error_model.json'), 'w') as f:
            json.dump(model_config, f, indent=4)

    optimizer = tf.keras.optimizers.Adam(train_config.lr)

    model.compile(optimizer=optimizer,
                  loss={'sed_out': tf.keras.losses.BinaryCrossentropy(),
                        'doa_out': tf.keras.losses.MSE},
                  loss_weights=[1, 1000])
    performances = {}
    for epoch in range(train_config.epoch):
        history = model.fit(trainset,
                            validation_data=valset).history
        if len(performances) == 0:
            for k, v in history.items():
                performances[k] = v
        else:
            for k, v in history.items():
                performances[k] += v

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
        if 'val_error_rate' in performances.keys():
            for k, v in scores.items():
                performances[k].append(v)
        else:
            for k, v in scores.items():
                performances[k] = [v]

    performances.update({
        'flops': get_flops(model),
        'size': get_model_size(model)
    })
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


def main():
    train_config = args.parse_args()
    os.environ['CUDA_VISIBLE_DEVICES'] = train_config.gpus
    writer = Writer(train_config)
    if train_config.config:
        train_config = vars(train_config)
        train_config.update(writer.load(os.path.join(os.path.join('result', train_config['name']), 'train_config.json')))
        train_config = argparse.Namespace(**train_config)
    # if train_config.gpus != '-1':
    #     gpus = tf.config.experimental.list_physical_devices('GPU')
    #     print(gpus)
    #     if gpus:
    #         try:
    #             tf.config.experimental.set_virtual_device_configuration(
    #                 gpus[0],
    #                 [tf.config.experimental.VirtualDeviceConfiguration(
    #                     memory_limit=10240)])
    #         except RuntimeError as e:
    #             print(e)
    del train_config.gpus
    del train_config.config
    del train_config.new

    name = train_config.name
    if name.endswith('.json'):
        name = os.path.splitext(name)[0]

    input_shape = [300, 64, 7]

    # datasets
    trainset = get_dataset(train_config, mode='train').prefetch(tf.data.experimental.AUTOTUNE)
    valset = get_dataset(train_config, mode='val').prefetch(tf.data.experimental.AUTOTUNE)

    # input shape
    input_shape = [x.shape for x, _ in trainset.take(1)][0]
    print(f'input_shape: {input_shape}')

    # Evaluator
    evaluator = SELDMetrics(doa_threshold=20, n_classes=train_config.n_classes)

    if not os.path.exists(writer.train_config_path):
        writer.train_config_dump()
    else:
        loaded_train_config = writer.train_config_load()
        if loaded_train_config != vars(train_config):
            for k, v in vars(train_config).items():
                print(k, ':', v)
            raise ValueError('train config doesn\'t match')

    
    while True:
        writer.index += 1 # 차수

        current_result_path = os.path.join(writer.result_path, f'result_{writer.index}.json')
        results = []

        # resume
        if os.path.exists(current_result_path):
            results = writer.load(current_result_path)
        # search space
        search_space_path = os.path.join(writer.result_path, f'search_space_{writer.index}.json')
        if os.path.exists(search_space_path):
            search_space = writer.load(search_space_path)
        else:
            # search space initializing
            specific_search_space = {'num2d': block_2d_num, 'num1d': block_1d_num}
            for i in range(specific_search_space['num2d'][-1] + specific_search_space['num1d'][-1]):
                specific_search_space[f'BLOCK{i}'] = {
                    'search_space_2d': search_space_2d,
                    'search_space_1d': search_space_1d,
                }

            specific_search_space['SED'] = {'search_space_1d': search_space_1d}
            specific_search_space['DOA'] = {'search_space_1d': search_space_1d}
            search_space = specific_search_space
            writer.dump(search_space, search_space_path)

        current_number = len(results)
        for number in range(current_number, train_config.n_samples):
            model_configs = get_config(train_config, search_space, input_shape=input_shape, postprocess_fn=postprocess_fn)

            # 학습
            start = time.time()
            outputs = train_and_eval(
                train_config, model_configs, 
                input_shape, 
                trainset, valset, evaluator)
            outputs['time'] = time.time() - start

            # eval
            outputs['objective_score'] = get_objective_score(outputs)

            # 결과 저장
            results.append({'config': model_configs, 'perf': outputs})
            writer.dump(results, current_result_path)
        
        
        # 분석
        check = True
        table = analyzer(search_space, results, train_config)
        tmp_table = list(filter(lambda x: x[0][0] <= train_config.threshold and x[-2] != 'identity_block', table))
        if len(tmp_table) == 0:
            print('MODEL SEARCH COMPLETE!!')
            return

        while check:
            table = analyzer(search_space, results, train_config)
            # 단순히 좁힐 게 있는 지 탐지
            tmp_table = list(filter(lambda x: x[0][0] <= train_config.threshold and x[-2] != 'identity_block', table))
            # search space 줄이기
            check, search_space, results = narrow_search_space(search_space, table, results, train_config, writer)


if __name__=='__main__':
    main()

