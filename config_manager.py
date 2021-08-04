import json, argparse, os, pdb

def save_config(path, name, config:dict):
    jsonpath = os.path.join(path, name)
    print(f'Save config as {name}')
    with open(jsonpath, 'w') as f:
        json.dump(config, f, sort_keys=True, indent=4)

def load_config(path, name):
    if not (os.path.splitext(name)[-1] == '.json'):
        name += '.json'
    jsonpath = os.path.join(path, name)
    if os.path.exists(jsonpath): # name.json exists
        with open(jsonpath, 'r') as f:
            res = json.load(f)
        return res
    else:
        print(f"{jsonpath} config don't exists")
        raise ValueError()

def manage_version(path, name):
    from glob import glob
    configs = sorted(glob('_'.join(os.path.splitext(os.path.join(path, name))[0].split('_')[:-1]) + '*'))
    latest = os.path.splitext(os.path.basename(configs[-1]))[0]
    oldversion = int(latest.split('v_')[-1])
    newversion = str(oldversion + 1)
    name = name.replace(f"_{'_'.join(os.path.splitext(os.path.join(path, name))[0].split('_')[-2:])}", f'_v_{newversion}')
    return name

def find_duplicate_config(jsonpath, newconfig, mode):
    from glob import glob
    name = get_name(jsonpath)
    configs = glob(os.path.dirname(os.path.abspath(jsonpath)) + f'/{name}*.json')

    find = True
    for i in configs:
        _config = load_config(os.path.dirname(i), os.path.basename(i))
        _config = manage_mode(_config, mode)
        _config = manage_gpu(_config)

        if len(newconfig.keys()) != len(_config.keys()):
            continue
        
        for key in newconfig.keys():
            if key == 'name':
                continue
            if newconfig[key] != _config[key]:
                find = False
                break
        if find:
            return os.path.splitext(os.path.basename(i))[0]
    return False

def get_name(name):
    return os.path.basename(os.path.splitext(name)[0].split('_v_')[0])

def manage_gpu(config):
    try:
        os.environ['CUDA_VISIBLE_DEVICES'] = config['gpus']

        # divide gpu ram
        # import tensorflow as tf
        # gpus = tf.config.experimental.list_physical_devices('GPU')
        # print(gpus)
        # if gpus:
        #     try:
        #         tf.config.experimental.set_virtual_device_configuration(
        #             gpus[0],
        #             [tf.config.experimental.VirtualDeviceConfiguration(
        #                 memory_limit=10240)])
        #     except RuntimeError as e:
        #         print(e)
        del config['gpus']
    except:
        pass
    return config

def over_write_config(loaded_config, config):
    ''' overwrite config to loaded_config '''
    for key in config.keys():
        loaded_config[key] = config[key]
    return loaded_config

def manage_mode(config, mode):
    for key in config.keys():
        if config[key] == mode:
            del config[key]
            break
    return config

def get_config(name:str, 
              config:argparse.Namespace, 
              path:str='./config', 
              mode:str=''):
    '''
    name: CONFIG.json name
    config: parsed config
    use_only_saved: if True, ignore your typed configuration
    path: CONFIG.json path
    mode: (l: loading config, o: only use loaded config), default is ''
    '''
    print('---------------- config manager start ----------------')
    modes = ('l', 'o')
    if mode == 'o':
        print("WARINING: you didn't load config.")
    assert len(name) > 0, 'name must be typed'
    for i in mode:
        assert i in modes, 'mode must be l, o, lo, or ol'

    if not os.path.exists(path): # directory manage
        os.makedirs(path)
        print(f'configuration directory is made at {path}')
    initial_config = config
    config = vars(config) # gpus delete
    config = manage_mode(config, mode)
    config = manage_gpu(config)
    
    name = name + '.json' if not('.json' in name) else name
    while True:
        if 'l' in mode:
            if not os.path.exists(os.path.join(path, name)):
                print(f'There is no {os.path.splitext(name)[0]} config')
                raise ValueError()
            loaded_config = load_config(path, name)
            final_config = over_write_config(loaded_config, config)

            if 'o' in mode:
                print(f'Loaded config name: {name}')
                print(f'Only use it')
                
                final_config = loaded_config
                break
        elif 'o' in mode:
            print("You can't use only saved config without loading")
            raise ValueError()
        else:
            final_config = config
            name = os.path.splitext(name)[0] + '_v_0.json'
            final_config['name'] = os.path.splitext(name)[0]
            if not os.path.exists(os.path.join(path, name)):
                save_config(path, name, final_config)
            break
        
        dup = find_duplicate_config(os.path.join(path, name), final_config, mode)
        if dup:
            print(dup, 'is the same config with your final config and change name')
            name = os.path.splitext(os.path.basename(dup))[0]
            final_config['name'] = os.path.splitext(name)[0]
            break
        name = manage_version(path, name)
        final_config['name'] = os.path.splitext(name)[0]
        save_config(path, name, final_config)
        break
    print('----------------- config manager end -----------------')
    return argparse.Namespace(**final_config)
        


if __name__ == "__main__":
    import sys
    arg = argparse.ArgumentParser()
    arg.add_argument('--hi', type=str, default='bye')
    config = arg.parse_known_args(sys.argv[1:])[0]
    get_config('b_v_0)', config, False)