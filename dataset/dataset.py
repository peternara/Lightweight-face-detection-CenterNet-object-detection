from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import torch.utils.data as data
import numpy as np
import torch
import json
import cv2
import os
try:

    from .image import flip, color_aug
    from .image import get_affine_transform, affine_transform
    from .image import gaussian_radius, draw_umich_gaussian, draw_msra_gaussian
    from .image import draw_dense_reg
except:
    from image import flip, color_aug
    from image import get_affine_transform, affine_transform
    from image import gaussian_radius, draw_umich_gaussian, draw_msra_gaussian
    from image import draw_dense_reg
import math
import matplotlib.pyplot as plt
class CenterFaceData(data.Dataset):
    mean = np.array([0.40789654, 0.44719302, 0.47026115],
                                     dtype=np.float32).reshape(1, 1, 3)
    std  = np.array([0.28863828, 0.27408164, 0.27809835],
                                     dtype=np.float32).reshape(1, 1, 3)
    def __init__(self,txt_path, split= "train"):
        super(CenterFaceData, self).__init__()
        self.imgs_path = []
        self.words = []
        self.batch_count = 0
        self.img_size = 640
        self.split = split
        f = open(txt_path,'r')
        lines = f.readlines()
        isFirst = True
        labels = []
        for line in lines:
                line = line.rstrip() 
                if line.startswith('#'):
                        if isFirst is True:
                                isFirst = False
                        else:
                                labels_copy = labels.copy()
                                self.words.append(labels_copy)        
                                labels.clear()       
                        path = line[2:]
                        path = txt_path.replace('label.txt','images/') + path
                        self.imgs_path.append(path)

                else:
                        line = line.split(' ')
                        label = [float(x) for x in line]
                        labels.append(label)
        self.words.append(labels)
        self.default_resolution = [640, 640]
        self.not_rand_crop = False
        self.max_objs = 128
        self.keep_res = False
        self.down_ratio = 4
        self.mse_loss = False

        self._data_rng = np.random.RandomState(123)
        self._eig_val = np.array([0.2141788, 0.01817699, 0.00341571],
                                                         dtype=np.float32)
        self._eig_vec = np.array([
                [-0.58752847, -0.69563484, 0.41340352],
                [-0.5832747, 0.00994535, -0.81221408],
                [-0.56089297, 0.71832671, 0.41158938]
        ], dtype=np.float32)

    def _coco_box_to_bbox(self, box):
        bbox = np.array([box[0], box[1], box[0] + box[2], box[1] + box[3]],
                                        dtype=np.float32)
        return bbox
    def __len__(self):
        return len(self.imgs_path)    
    def _get_border(self, border, size):
        i = 1
        while size - border // i <= border // i:
                i *= 2
        return border // i

    def __getitem__(self, index):
        if os.path.exists(self.imgs_path[index]):
            img = cv2.imread(self.imgs_path[index])
        else:
            print("%s not exists"%self.imgs_path[index])
        anns = self.words[index]
        num_objs = min(len(anns), self.max_objs)
        height, width = img.shape[0], img.shape[1]
        c = np.array([img.shape[1] / 2., img.shape[0] / 2.], dtype=np.float32)
        s = max(img.shape[0], img.shape[1]) * 1.0
        input_h, input_w = self.default_resolution[0], self.default_resolution[1]

        flipped = False
        if self.split == 'train':
            s = s * np.random.choice(np.arange(0.6, 1.4, 0.1))
            w_border = self._get_border(128, img.shape[1])
            h_border = self._get_border(128, img.shape[0])
            c[0] = np.random.randint(low=w_border, high=img.shape[1] - w_border)
            c[1] = np.random.randint(low=h_border, high=img.shape[0] - h_border)

            if np.random.random() < 0.5:
                flipped = True
                img = img[:, ::-1, :]
                c[0] =  width - c[0] - 1
                
        trans_input = get_affine_transform(
            c, s, 0, [input_w, input_h])
        inp = cv2.warpAffine(img, trans_input, 
                         (input_w, input_h),
                         flags=cv2.INTER_LINEAR)
        inp = (inp.astype(np.float32) / 255.)
        # if self.split == 'train':
        color_aug(self._data_rng, inp, self._eig_val, self._eig_vec)
        inp = (inp - self.mean) / self.std
        inp = inp.transpose(2, 0, 1)

        output_h = input_h // self.down_ratio
        output_w = input_w // self.down_ratio
        num_classes = 1
        trans_output = get_affine_transform(c, s, 0, [output_w, output_h])

        hm = np.zeros((num_classes, output_h, output_w), dtype=np.float32)
        wh = np.zeros((self.max_objs, 2), dtype=np.float32)
        landmarks = np.zeros((self.max_objs, 10), dtype=np.float32)
        dense_wh = np.zeros((2, output_h, output_w), dtype=np.float32)
        reg = np.zeros((self.max_objs, 2), dtype=np.float32)
        ind = np.zeros((self.max_objs), dtype=np.int64)
        reg_mask = np.zeros((self.max_objs), dtype=np.uint8)

        draw_gaussian = draw_msra_gaussian if self.mse_loss else \
                                        draw_umich_gaussian
        
        cls_id = 0
        for k in range(num_objs):
            ann = anns[k]
            bbox = np.array(ann[:4].copy())
            lm = np.zeros( 10, dtype=np.float32)
            for i in range(5):
                if  self.split =='train' and ann[4]>0:
                    x = ann[4 + 3 * i]
                    y = ann[4 + 3 * i + 1]
                    lm[i*2] = x
                    lm[i*2+1] = y
                else:
                    lm[i*2] = -1
                    lm[i*2+1] = -1
            bbox = self._coco_box_to_bbox(bbox)
            if flipped:
                bbox[[0, 2]] = width - bbox[[2, 0]] - 1
                if lm[0] >=0:  
                    lm[0::2] = width - lm[0::2]
                    l_tmp = lm.copy()
                    lm[0:2] = l_tmp[2:4]
                    lm[2:4] = l_tmp[0:2]
                    lm[6:8] = l_tmp[8:10]
                    lm[8:10] = l_tmp[6:8]
            bbox[:2] = affine_transform(bbox[:2], trans_output)
            bbox[2:] = affine_transform(bbox[2:], trans_output)
            if lm[0] >=0:
                lm[:2] = affine_transform(lm[:2], trans_output)
                lm[2:4] = affine_transform(lm[2:4], trans_output)
                lm[4:6] = affine_transform(lm[4:6], trans_output)
                lm[6:8] = affine_transform(lm[6:8], trans_output)
                lm[8:10] = affine_transform(lm[8:10], trans_output)
            
            bbox[[0, 2]] = np.clip(bbox[[0, 2]], 0, output_w - 1)
            bbox[[1, 3]] = np.clip(bbox[[1, 3]], 0, output_h - 1)
            h, w = bbox[3] - bbox[1], bbox[2] - bbox[0]

            lm[0::2] -= bbox[0]
            lm[1::2] -= bbox[1]
            lm[[0, 2, 4, 6, 8]] = np.clip(lm[[0, 2, 4, 6, 8]], 1e-13, w)
            lm[[1, 3, 5, 7, 9]] = np.clip(lm[[1, 3, 5, 7, 9]], 1e-13, h)
            if h > 0 and w > 0:
                lm[0::2]/= w
                lm[1::2]/= h
                radius = gaussian_radius((math.ceil(h), math.ceil(w)))
                radius = max(0, int(radius))
                ct = np.array(
                    [(bbox[0] + bbox[2]) / 2, (bbox[1] + bbox[3]) / 2], dtype=np.float32)
                ct_int = ct.astype(np.int32)

                draw_umich_gaussian(hm[cls_id], ct_int, radius)
                wh[k] = 1. * w, 1. * h
                ind[k] = ct_int[1] * output_w + ct_int[0]
                reg[k] = ct - ct_int
                reg_mask[k] = 1
                landmarks[k] = lm
        # print(landmarks)
        ret = {'input': inp, 'hm': hm, 'lm':landmarks,'reg_mask': reg_mask, 'ind': ind, 'wh': wh, 'reg': reg}

        if not self.split == 'train':
            gt_det = np.zeros((self.max_objs, 4), dtype=np.float32)
            for k in range(num_objs):
                ann = anns[k]
                bbox = np.array(ann[:4].copy())
                bbox = self._coco_box_to_bbox(bbox)
                gt_det[k:4] = bbox

            gt_det = np.array(gt_det, dtype=np.float32) if len(gt_det) > 0 else \
                   np.zeros((1, 4), dtype=np.float32)
            meta = {'gt_det': gt_det, 'h': height, 'w': width}
            ret['meta'] = meta
        return ret

from torch.utils.data import Dataset, DataLoader
if __name__ == '__main__':
    data = CenterFaceData(txt_path = "/media/hdd/sources/data/data_wider/train/label.txt")
    dataloader_val = DataLoader(data, num_workers=8, batch_size=2,pin_memory=True,
            drop_last=False)
    for data in dataloader_val:
        print(data["hm"])