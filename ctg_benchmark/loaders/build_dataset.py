import csv
import glob
import requests
from dataclasses import dataclass
from typing import Union, List

import numpy as np
import zlib
import subprocess
import tarfile
import zipfile
import os
from torch_geometric.data import Data

from ctg_benchmark import default_dataset_file_list, urls
from ctg_benchmark.loaders.utils import collect_features, graph_preprocessing, map_nodes_labels, get_grs
from ctg_benchmark.transforms.basics import compute_to_torch_tensor
from ctg_benchmark.transforms.transforms import TransformFactory
from ctg_benchmark.utils.io import open_full_stack
from ctg_benchmark.utils.utils import get_basic_loader_config


@dataclass
class ConfigKeyChain:
    nodes_ids_key: str
    edges_ids_key: str
    nodes_labels_key: str
    edges_labels_key: str
    node_features_key: str
    edges_features_key: str
    pos_features_key: str
    config: dict
    register_plugin: list = None
    node_features_config: dict = None
    edges_features_config: dict = None
    pos_features_config: dict = None
    graph_data_config: dict = None

    def __post_init__(self):
        self.node_features_config = self.config.get('node_features')
        self.edges_features_config = self.config.get('edges_features')
        self.pos_features_config = self.config.get('pos_features', None)
        self.graph_data_config = self.config.get('graph_data', None)


def default_build_torch_geometric_data(data_file_path: str,
                                       config: dict = None,
                                       meta: dict = None) -> (Data, dict):
    config = config if config is not None else get_basic_loader_config('dataset')
    default_keys = [key_value for key_value in config['keys'].values()]
    stack = open_full_stack(data_file_path, keys=default_keys)

    key_config = ConfigKeyChain(config=config,
                                register_plugin=config.get('register_plugin', None),
                                **config['keys'])

    default_factory = TransformFactory(key_config.register_plugin) if key_config.register_plugin is not None else None
    # nodes feat
    node_features, node_slices = collect_features(stack.get(key_config.node_features_key),
                                                  list_configs=key_config.node_features_config,
                                                  transform_factory=default_factory)

    # edges feat
    edges_features, edges_slices = collect_features(stack.get(key_config.edges_features_key),
                                                    list_configs=key_config.edges_features_config,
                                                    transform_factory=default_factory)

    # pos feat
    if key_config.pos_features_config is not None:
        pos_features, pos_slices = collect_features(stack.get(key_config.pos_features_key),
                                                    list_configs=key_config.pos_features_config,
                                                    transform_factory=default_factory)
    else:
        pos_features, pos_slices = None, None

    # global graph processing
    (node_ids,
     edges_ids,
     edges_labels,
     edges_features) = graph_preprocessing(stack.get(key_config.nodes_ids_key),
                                           stack.get(key_config.edges_ids_key),
                                           stack.get(key_config.edges_labels_key),
                                           edges_features)

    # nodes to ignore should be implemented as a node mask
    node_labels, nodes_to_ignore = map_nodes_labels(stack.get(key_config.nodes_labels_key))
    if len(nodes_to_ignore) > 0:
        print("Masked nodes are not implemented")

    # grs
    origin, axis = get_grs(stack.get('attributes'))

    node_ids = compute_to_torch_tensor(node_ids, data_type='int')
    node_labels = compute_to_torch_tensor(node_labels, data_type='int')
    edges_ids = compute_to_torch_tensor(edges_ids, data_type='int').T
    edges_labels = compute_to_torch_tensor(edges_labels, data_type='int')

    origin = compute_to_torch_tensor(origin, data_type='float')
    axis = compute_to_torch_tensor(axis, data_type='float')

    # build torch_geometric Data obj
    graph_data = Data(x=node_features,
                      x_slices=node_slices,
                      y=node_labels,
                      pos=pos_features,
                      pos_slices=pos_slices,
                      origin=origin,
                      axis=axis,
                      file_path=data_file_path,
                      metadata=meta,
                      node_ids=node_ids,
                      edge_attr=edges_features,
                      edge_slices=edges_slices,
                      edge_y=edges_labels,
                      edge_index=edges_ids,
                      in_edges_attr=edges_features.shape[1],
                      in_features=node_features.shape[1],
                      num_nodes=node_features.shape[0])
    return graph_data, config


def sort_files_by_stage(path):
    with open(path, 'r') as f:
        reader = csv.DictReader(f)
        sorted_dataset = {}
        for line in reader:
            if line['stage'] in sorted_dataset:
                sorted_dataset[line['stage']].append(line['stack'])
            else:
                sorted_dataset[line['stage']] = [line['stack']]
    return sorted_dataset


def sort_files(source_root: Union[str, List[str]],
               file_list_path: str = None):
    file_list_path = default_dataset_file_list if file_list_path is None else file_list_path
    dataset_config_sorted = sort_files_by_stage(file_list_path)

    if isinstance(source_root, str):
        source_root = [source_root]

    dataset_full = {}
    for key, stack_names_list in dataset_config_sorted.items():
        dataset_key = {}
        for stack_name in stack_names_list:
            _stack_name = stack_name.replace('.h5', '*.h5')
            _stack_name_no_ext = stack_name.replace('.h5', '')
            stack_idx = f'{key}_{_stack_name_no_ext}'

            full_stack_paths = []
            for _source_root in source_root:
                full_stack_paths += sorted(glob.glob(os.path.join(_source_root, key, _stack_name)))

            dataset_key[stack_idx] = []

            for i, stack_path in enumerate(full_stack_paths):
                base = os.path.split(os.path.split(stack_path)[0])[0]
                unique_idx = f'{key}_{_stack_name_no_ext}_{i}'
                dataset_key[stack_idx].append({'stage': key,
                                               'stack': stack_name,
                                               'stack_idx': stack_idx,
                                               'multiplicity': str(i),
                                               'base': base,
                                               'unique_idx': unique_idx,
                                               'path': stack_path})

        dataset_full[key] = dataset_key
    return dataset_full


def append_stack_ids(split, stage_dict):
    split_list = []
    for _stack_idx in split:
        list_stack_idx_mul = stage_dict[_stack_idx]
        for stack_ids in list_stack_idx_mul:
            split_list.append(stack_ids)
    return split_list


def build_cv_splits(source_root: Union[str, List[str]],
                    file_list_path: str = None,
                    number_splits: int = 5,
                    seed: int = 0) -> dict:
    dataset_full = sort_files(source_root, file_list_path)

    splits = {i: {'val': [], 'train': []} for i in range(number_splits)}
    for stage, stage_dict in dataset_full.items():
        stage_list = list(stage_dict.keys())
        np.random.seed(seed)
        np.random.shuffle(stage_list)

        for i in range(number_splits):
            stage_split = np.array_split(stage_list, number_splits)

            test_split = stage_split.pop(i).tolist()

            train_split = []
            for _split in stage_split:
                train_split += _split.tolist()

            splits[i]['train'] += append_stack_ids(train_split, stage_dict)
            splits[i]['val'] += append_stack_ids(test_split, stage_dict)
    return splits


def build_std_splits(source_root: Union[str, List[str]],
                     splits_ratios=(0.6, 0.1, 0.3),
                     file_list_path: str = None,
                     seed: int = 0) -> dict:
    assert np.allclose(np.sum(splits_ratios), 1)
    dataset_full = sort_files(source_root, file_list_path)

    splits = {'train': [], 'val': [], 'test': []}
    for stage, stage_dict in dataset_full.items():
        stage_list = list(stage_dict.keys())
        np.random.seed(seed)
        np.random.shuffle(stage_list)
        _splits_ratios = [np.ceil(ratio * len(stage_list)) for ratio in splits_ratios]
        _splits_ratios = np.cumsum(_splits_ratios)[:-1].astype('int64')
        stage_list = np.array(stage_list)
        train_split, val_split, test_split = np.split(stage_list, _splits_ratios)

        splits['train'] += append_stack_ids(train_split, stage_dict)
        splits['val'] += append_stack_ids(val_split, stage_dict)
        splits['test'] += append_stack_ids(test_split, stage_dict)
    return splits


def _un_tar(file_path, root):
    with tarfile.open(file_path) as tar_f:
        tar_f.extractall(path=root)


def _un_zip(file_path, root):
    with zipfile.ZipFile(file_path) as zip_f:
        zip_f.extractall(path=root)


def request_dataset(url, out_file):
    with requests.get(url, allow_redirects=True) as r:
        with open(out_file, 'wb') as f:
            f.write(r.content)


def download_dataset(root,
                     dataset_name='label_grs_surface',
                     mode='zip'):
    os.makedirs(root, exist_ok=True)
    url = urls.get(dataset_name, None)
    if url is None:
        raise ValueError(f"Dataset {dataset_name} does not exist")

    if mode == 'zip':
        ext = '.zip'
    elif mode == 'tar':
        ext = '.tar.xz'
    else:
        raise NotImplementedError

    file_path = os.path.join(root, f'{dataset_name}{ext}')
    print(f'Downloading {dataset_name} in {file_path}... this will take several minutes')
    request_dataset(url=url, out_file=file_path)

    print(f'Extracting {file_path}')
    if mode == 'zip':
        _un_zip(file_path, root)
    elif mode == 'tar':
        _un_tar(file_path, root)
    else:
        raise NotImplementedError

    print(f'Deleting {file_path}')
    out = subprocess.run(['rm', file_path])
    assert out.returncode == 0


def change_base_dataset():
    pass