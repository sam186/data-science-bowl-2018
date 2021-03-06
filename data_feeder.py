import random
from collections import defaultdict

import sys
import logging

import numpy as np
import os
import cv2
import time
from scipy import ndimage
from tensorpack.dataflow.common import BatchData, MapData, MapDataComponent
from tensorpack.dataflow.base import RNGDataFlow
from tensorpack.dataflow import PrefetchData

from data_augmentation import data_to_segment_input, data_to_normalize01
from hyperparams import HyperParams

logger = logging.getLogger('train')
logger.setLevel(logging.DEBUG)
ch = logging.StreamHandler(sys.stdout)
ch.setLevel(logging.DEBUG)
formatter = logging.Formatter('[%(asctime)s] [%(name)s] [%(levelname)s] %(message)s')
ch.setFormatter(formatter)
logger.handlers = []
logger.addHandler(ch)

if HyperParams.get().dataset_stage == 1:
    master_dir_train = '/data/public/rw/datasets/dsb2018/train'
    master_dir_train2 = None
    master_dir_test = '/data/public/rw/datasets/dsb2018/test'

    # train/valid set k folds implementation
    IDX_LIST = list(next(os.walk(master_dir_train))[1])

    logger.info('Loading Dataset for Stage1(%d)' % (len(IDX_LIST)))

    if HyperParams.get().data_fold > 0:
        VALID_IDX_LIST = IDX_LIST[-94*HyperParams.get().data_fold:][:94]
        TRAIN_IDX_LIST = sorted(list(set(IDX_LIST) - set(VALID_IDX_LIST)))
        assert len(VALID_IDX_LIST) == 94, len(VALID_IDX_LIST)
        assert len(TRAIN_IDX_LIST) == 576, len(TRAIN_IDX_LIST)
    else:
        # to test all images
        VALID_IDX_LIST = IDX_LIST
        TRAIN_IDX_LIST = []
        assert len(VALID_IDX_LIST) == 670, len(VALID_IDX_LIST)
        assert len(TRAIN_IDX_LIST) == 0, len(TRAIN_IDX_LIST)
else:
    master_dir_train = '/data/public/rw/datasets/dsb2018/train'
    master_dir_train2 = '/data/public/rw/datasets/dsb2018/test_stage1'
    master_dir_test = '/data/public/rw/datasets/dsb2018/stage2_test_final'

    # train/valid set k folds implementation
    IDX_LIST = list(next(os.walk(master_dir_train))[1])
    IDX_LIST2 = list(next(os.walk(master_dir_train2))[1])

    logger.info('Loading Dataset for Stage2(%d+%d)' % (len(IDX_LIST), len(IDX_LIST2)))

    if HyperParams.get().data_fold > 0:
        VALID_IDX_LIST = IDX_LIST[-98 * HyperParams.get().data_fold:][:98]
        TRAIN_IDX_LIST = sorted(list(set(IDX_LIST) - set(VALID_IDX_LIST)))
        VALID_IDX_LIST += IDX_LIST2[-14 * HyperParams.get().data_fold:][:14]
        TRAIN_IDX_LIST += sorted(list(set(IDX_LIST2) - set(VALID_IDX_LIST)))
        assert len(VALID_IDX_LIST) == 98 + 14, len(VALID_IDX_LIST)
        assert len(TRAIN_IDX_LIST) == 572 + 51, len(TRAIN_IDX_LIST)
    else:
        # to test all test images in stage1
        VALID_IDX_LIST = IDX_LIST2
        TRAIN_IDX_LIST = []
        assert len(VALID_IDX_LIST) == 65, len(VALID_IDX_LIST)
        assert len(TRAIN_IDX_LIST) == 0, len(TRAIN_IDX_LIST)
TEST_IDX_LIST = list(next(os.walk(master_dir_test))[1])

# extra1 ref : https://www.kaggle.com/voglinio/external-h-e-data-with-mask-annotations/notebook
# extra2 ref : https://www.kaggle.com/branislav1991/converting-tnbc-external-data-to-dsb2018-format/
extra1_dir = '/data/public/rw/datasets/dsb2018/extra_data'
extra2_dir = '/data/public/rw/datasets/dsb2018/extra_data_tnbc'


class CellImageData:
    def __init__(self, target_id, path, ext='png'):
        self.target_id = target_id

        # read
        if '/' in target_id:
            target_dir = ''
            img_path = target_id
        else:
            target_dir = os.path.join(path, target_id)
            img_path = os.path.join(target_dir, 'images', target_id + '.' + ext)

        for _ in range(10):
            self.img = cv2.imread(img_path, cv2.IMREAD_COLOR)
            if self.img is not None and self.img.shape[0] > 0:
                break
            logger.warning('%s %s %s not read' % (target_id, ext, img_path))
            time.sleep(1)
        self.img_h, self.img_w = self.img.shape[:2]
        assert self.img_h > 0 and self.img_w > 0
        self.masks = []
        self.mask_h, self.mask_w = 0, 0
        mask_dir = os.path.join(target_dir, 'masks')

        if not os.path.exists(mask_dir):
            return

        for mask_file in next(os.walk(mask_dir))[2]:
            mask_path = os.path.join(target_dir, 'masks', mask_file)
            mask = None
            for _ in range(10):
                mask = cv2.imread(mask_path, cv2.IMREAD_GRAYSCALE)
                if mask is None:
                    logger.warning('%s %s %s not read' % (target_id, ext, mask_file))
                else:
                    break
                time.sleep(1)
            if mask is None:
                continue
            mask = mask >> 7    # faster than mask // 129
            self.masks.append(mask)

    def remove_redundant_masks(self):
        if len(self.masks) > 0:
            self.mask_h, self.mask_w = self.masks[0].shape[:2]
            self.masks = [mask for mask in self.masks if np.max(mask) > 0]

    def single_mask(self, ch1=True):
        """
        :return: (h, w, 1) numpy
        """
        self.remove_redundant_masks()
        multi_masks = self.multi_masks()
        multi_masks = np.sum(multi_masks, axis=2)
        if ch1:
            multi_masks = multi_masks[..., np.newaxis]
        return multi_masks

    def multi_masks(self, transpose=True):
        """
        :return: (h, w, m) numpy
        """
        self.remove_redundant_masks()
        if len(self.masks) == 0:
            if self.mask_h > 0 and self.mask_w > 0:
                img_h, img_w = self.mask_h, self.mask_w
            else:
                img_h, img_w = 228, 228  # TODO : temporal code
            r = np.zeros((1, img_h, img_w))
        else:
            r = np.stack(self.masks, axis=0)
        if transpose:
            r = r.transpose([1, 2, 0])
        return r

    def multi_masks_batch(self):
        self.remove_redundant_masks()
        if len(self.masks) > 0:
            img_h, img_w = self.masks[0].shape[:2]
        elif self.mask_h > 0 and self.mask_w > 0:
            img_h, img_w = self.mask_h, self.mask_w
        else:
            img_h, img_w = 228, 228  # TODO : temporal code
        m = np.zeros(shape=(img_h, img_w, 1), dtype=np.uint8)
        for idx, mask in enumerate(self.masks):
            m = m + mask[..., np.newaxis] * (idx + 1)
        return m

    def image(self, is_gray=True):
        """
        :return: (h, w, 3) or (h, w, 1) numpy
        """
        img = self.img
        if is_gray:
            img = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
            img = img[..., np.newaxis]
        return img

    def unet_weights(self):
        # ref : https://www.kaggle.com/piotrczapla/tensorflow-u-net-starter-lb-0-34/notebook
        if len(self.masks) == 0:
            if self.mask_h > 0 and self.mask_w > 0:
                img_h, img_w = self.mask_h, self.mask_w
            else:
                img_h, img_w = 228, 228  # TODO : temporal code
            return np.ones((img_h, img_w, 1), dtype=np.float32)

        w0 = 10
        sigma = 5

        merged_mask = self.single_mask(ch1=False)

        distances = np.array([ndimage.distance_transform_edt(m == 0) for m in self.masks])
        shortest_dist = np.sort(distances, axis=0)

        # distance to the border of the nearest cell
        d1 = shortest_dist[0]
        # distance to the border of the second nearest cell
        d2 = shortest_dist[1] if len(shortest_dist) > 1 else np.zeros(d1.shape)

        weight = w0 * np.exp(-(d1 + d2) ** 2 / (2 * sigma ** 2)).astype(np.float32)
        weight = 1 + (merged_mask == 0) * weight
        return weight[..., np.newaxis]


class CellImageDataManager(RNGDataFlow):
    def __init__(self, name, path, idx_list, is_shuffle=False):
        self.name = name
        self.path = path
        self.idx_list = idx_list
        self.is_shuffle = is_shuffle
        logger.info('%s data size = %d' % (self.name, self.size()))

    def size(self):
        return len(self.idx_list)

    def get_data(self):
        if self.is_shuffle:
            random.shuffle(self.idx_list)

        for idx in self.idx_list:
            if 'TCGA' in idx:
                # extra1 dataset
                yield [CellImageData(idx, extra1_dir, ext='tif')]
            elif 'TNBC' in idx:
                yield [CellImageData(idx, extra2_dir, ext='png')]
            elif idx in IDX_LIST2:
                yield [CellImageData(idx, master_dir_train2, ext='png')]
            else:
                # default dataset
                yield [CellImageData(idx, self.path)]


class CellImageDataManagerTrain(CellImageDataManager):
    LIST_ORIG = TRAIN_IDX_LIST
    LIST_EXT1 = list(next(os.walk(extra1_dir))[1])[:-9]
    LIST_EXT2 = list(next(os.walk(extra2_dir))[1])[:-5]
    LIST = LIST_ORIG + LIST_EXT1 # + LIST_EXT2

    def __init__(self):
        super().__init__(
            'train',
            master_dir_train,
            CellImageDataManagerTrain.LIST,
            True
        )


class CellImageDataManagerValid(CellImageDataManager):
    LIST_ORIG = VALID_IDX_LIST
    LIST_EXT1 = list(next(os.walk(extra1_dir))[1])[-9:]
    LIST_EXT2 = list(next(os.walk(extra2_dir))[1])[-5:]
    LIST = LIST_EXT2 + LIST_EXT1 + LIST_ORIG

    def __init__(self):
        super().__init__(
            'valid',
            master_dir_train,
            CellImageDataManagerValid.LIST,
            False
        )


class CellImageDataManagerTest(CellImageDataManager):
    LIST = TEST_IDX_LIST

    def __init__(self):
        super().__init__(
            'test',
            master_dir_test,
            CellImageDataManagerTest.LIST,
            False
        )


class MetaData:
    # Here will be the instance stored.
    __instance = None

    @staticmethod
    def get():
        """ Static access method. """
        if MetaData.__instance is None:
            MetaData()
        return MetaData.__instance

    @staticmethod
    def read_cluster(path):
        cluster = {}
        with open(path, 'r') as f:
            for idx, line in enumerate(f.readlines()):
                if idx == 0:
                    continue
                elms = line.split(',')
                cluster[elms[0]] = int(elms[1])
        return cluster

    def __init__(self):
        """ Virtually private constructor. """
        if MetaData.__instance is not None:
            raise Exception("This class is a singleton!")
        else:
            MetaData.__instance = self

        self.train_cluster = MetaData.read_cluster('./metadata/share_train_df.csv')
        self.test_cluster = MetaData.read_cluster('./metadata/share_test_df.csv')


def get_default_dataflow():
    ds = CellImageDataManagerTrain()
    ds = PrefetchData(ds, 1000, 12)

    return ds


def get_default_dataflow_batch(batchsize=32):
    ds = get_default_dataflow()
    ds = MapData(ds, data_to_segment_input)
    ds = BatchData(ds, batchsize)
    ds = MapDataComponent(ds, data_to_normalize01)
    ds = PrefetchData(ds, 10, 2)

    return ds


def batch_to_multi_masks(multi_masks_batch, transpose=True):
    a = np.array([multi_masks_batch == (idx + 1) for idx in range(np.max(multi_masks_batch))], dtype=np.uint8)

    if transpose:
        return a[..., 0].transpose([1, 2, 0])
    else:
        return a[..., 0]


if __name__ == '__main__':
    split_idx = 576
    print('total size=', len(list(next(os.walk(master_dir_train))[1])))     # 670
    train_set = list(next(os.walk(master_dir_train))[1])[-576:]
    valid_set = list(next(os.walk(master_dir_train))[1])[:-576]
    test_set = list(next(os.walk(master_dir_test))[1])

    sys.exit(0)

    ds = get_default_dataflow()
    print(dir(ds))
    print(dir(ds.get_data()))
    pass

    def histogram(set, cluster_info):
        hist = defaultdict(lambda: 0)
        for data in set:
            # print(train_data)
            try:
                cluster_id = cluster_info[data]
            except:
                print(data)
                cluster_id = 1
            hist[cluster_id] += 1
        print(hist)

    histogram(train_set, MetaData.get().train_cluster)
    histogram(valid_set, MetaData.get().train_cluster)
    histogram(test_set, MetaData.get().test_cluster)
