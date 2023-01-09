import cv2.ximgproc.segmentation

import math
import heapq
import random
import itertools

import torch
import torch.nn.functional as F
import torchvision.transforms.functional as TF

def rgb_to_hsv(image, eps: float = 1e-6):
    # https://github.com/kornia/kornia/blob/master/kornia/color/hsv.py
    maxc, _ = image.max(-3)
    maxc_mask = image == maxc.unsqueeze(-3)
    _, max_indices = ((maxc_mask.cumsum(-3) == 1) & maxc_mask).max(-3)
    minc = image.min(-3)[0]

    v = maxc  # brightness

    deltac = maxc - minc
    s = deltac / (v + eps)

    # avoid division by zero
    deltac = torch.where(deltac == 0, torch.ones_like(deltac, device=deltac.device, dtype=deltac.dtype), deltac)

    maxc_tmp = maxc.unsqueeze(-3) - image
    rc, gc, bc = maxc_tmp.unbind(dim = -3)

    h = torch.stack([bc - gc, 2.0 * deltac + rc - bc, 4.0 * deltac + gc - rc], dim=-3)

    h = torch.gather(h, dim=-3, index=max_indices[..., None, :, :])
    h = h.squeeze(-3)
    h = h / deltac

    h = (h / 6.0) % 1.0

    h = 2 * math.pi * h

    return torch.stack([h, s, v], dim=-3)

def rgb_to_lab(image):
    # https://github.com/kornia/kornia/blob/master/kornia/color/lab.py
    
    def rgb_to_xyz(image):
        # https://github.com/kornia/kornia/blob/master/kornia/color/xyz.py
        r, g, b = image.unbind(dim = -3)
        x = 0.412453 * r + 0.357580 * g + 0.180423 * b
        y = 0.212671 * r + 0.715160 * g + 0.072169 * b
        z = 0.019334 * r + 0.119193 * g + 0.950227 * b
        return torch.stack([x, y, z], -3)

    def rgb_to_linear_rgb(image):
        # https://github.com/kornia/kornia/blob/master/kornia/color/rgb.py
        return torch.where(image > 0.04045, torch.pow(((image + 0.055) / 1.055), 2.4), image / 12.92)

    # Convert begin sRGB to Linear RGB
    lin_rgb = rgb_to_linear_rgb(image)

    xyz_im = rgb_to_xyz(lin_rgb)

    # normalize for D65 white point
    xyz_ref_white = torch.tensor([0.95047, 1.0, 1.08883], device=xyz_im.device, dtype=xyz_im.dtype)[..., :, None, None]
    xyz_normalized = torch.div(xyz_im, xyz_ref_white)

    threshold = 0.008856
    power = torch.pow(xyz_normalized.clamp(min=threshold), 1 / 3.0)
    scale = 7.787 * xyz_normalized + 4.0 / 29.0
    xyz_int = torch.where(xyz_normalized > threshold, power, scale)

    x, y, z = xyz_int.unbind(dim = -3)

    L = (116.0 * y) - 16.0
    a = 500.0 * (x - y)
    b = 200.0 * (y - z)

    return torch.stack([L, a, b], dim=-3)

def rgb_to_grayscale(image, rgb_weights = [0.299, 0.587, 0.114]):
    # https://github.com/kornia/kornia/blob/master/kornia/color/gray.py
    r, g, b = image.unsqueeze(-4).unbind(dim = -3)
    return rgb_weights[0] * r + rgb_weights[1] * g + rgb_weights[2] * b

def image_scharr_gradients(img : 'BCHW') -> 'BC2HW':
    flipped_scharr_x = torch.tensor([
        [-3,  0, 3 ],
        [-10, 0, 10],
        [-3,  0, 3 ]
    ], dtype = img.dtype, device = img.device)
    kernel = torch.stack([flipped_scharr_x, flipped_scharr_x.t()]).unsqueeze(1)
    return F.conv2d(img.flatten(end_dim = -3).unsqueeze(1), kernel, padding = 1).unflatten(0, img.shape[:-2])

def image_gaussian_grads(img):
    grads = image_scharr_gradients(img)
    
    img_height, img_width = img.shape[-2:]
    xywh = rotated_xywh(img_height, img_width, 45.0)
    startx, starty = int(max(0, (xywh[-2] - img_width) / 2)), int(max(0, (xywh[-1] - img_height) / 2))
    img_rotated = TF.rotate(img, 45.0, expand = True)
    grads_rotated = image_scharr_gradients(img_rotated)
    grads_rotated = TF.rotate(grads_rotated.flatten(end_dim = -3), -45.0, expand = True).unflatten(0, grads_rotated.shape[:-2])
    grads_rotated = grads_rotated[..., starty : starty + img_height, startx : startx + img_width]

    return torch.cat([grads.clamp(min = 0), grads.clamp(max = 0), grads_rotated.clamp(min = 0), grads_rotated.clamp(max = 0)], dim = -3)

def normalize_min_max(x, dim, eps = 1e-12):
    hmin, hmax = x.amin(dim = dim, keepdim = True), x.amax(dim = dim, keepdim = True)
    return (x - hmin) / (eps + hmax - hmin) 
        
def expand_dim(tensor, expand, dim):
    return tensor.unsqueeze(dim).expand((-1, ) * (dim if dim >= 0 else tensor.ndim + dim + 1) + (expand, ) + (-1, ) * (tensor.ndim - (dim if dim >= 0 else tensor.ndim + dim + 1)))

def expand_ones_like(tensor, dtype = torch.float32):
    return torch.ones(1, device = tensor.device, dtype = dtype).expand_as(tensor)

def rotated_xywh(img_height, img_width, angle = 45.0, scale = 1.0):
    # https://docs.opencv.org/4.5.3/da/d54/group__imgproc__transform.html#gafbbc470ce83812914a70abfb604f4326
    
    center = (img_width / 2.0, img_height / 2.0)
    alpha, beta = scale * math.cos(math.radians(angle)), scale * math.sin(math.radians(angle))
    rot = torch.tensor([
            [alpha, beta, (1 - alpha) * center[0] - beta * center[1]],
            [-beta, alpha, beta * center[0] + (1 - alpha) * center[1]]
        ])
    rotate = lambda point: (rot @ torch.tensor((point[0], point[1], 1.0), dtype = torch.float32)).tolist()[:2]

    points = list(map(rotate, [(0, 0), (img_width - 1, 0), (0, img_height - 1), (img_width - 1, img_height - 1)]))

    x1, y1 = min(x for x, y in points), min(y for x, y in points)
    x2, y2 = max(x for x, y in points), max(y for x, y in points)
    xywh = (x1, y1, x2 - x1 + 1, y2 - y1 + 1)

    return xywh

def bbox_merge_tensor(xywh1, xywh2):
    xmin, ymin = torch.min(xywh1[..., 0], xywh2[..., 0]), torch.min(xywh1[..., 1], xywh2[..., 1])
    xmax, ymax = torch.max(xywh1[..., 0] + xywh1[..., 2] - 1, xywh2[..., 0] + xywh2[..., 2] - 1), torch.max(xywh1[..., 1] + xywh1[..., 3] - 1, xywh2[..., 1] + xywh2[..., 3] - 1)
    return torch.stack([xmin, ymin, xmax - xmin + 1, ymax - ymin + 1], dim = -1)

def bbox_merge(xywh1, xywh2):
    x1, y1 = min(xywh1[0], xywh2[0]), min(xywh1[1], xywh2[1])
    x2, y2 = max(xywh1[0] + xywh1[2] - 1, xywh2[0] + xywh2[2] - 1), max(xywh1[1] + xywh1[3] - 1, xywh2[1] + xywh2[3] - 1)
    return (x1, y1, x2 - x1 + 1, y2 - y1 + 1)

class SelectiveSearch(torch.nn.Module):
    # https://github.com/opencv/opencv_contrib/blob/master/modules/ximgproc/src/selectivesearchsegmentation.cpp
    def __init__(self, base_k = 150, inc_k = 150, sigma = 0.8, min_size = 100, preset = 'fast', compute_region_rank = None, postprocess_labels = None):
        super().__init__()
        self.base_k = base_k
        self.inc_k = inc_k
        self.sigma = sigma
        self.min_size = min_size
        self.preset = preset
        self.compute_region_rank = compute_region_rank if compute_region_rank is not None else (lambda reg: reg['level'] * random.random())
        self.postprocess_labels = postprocess_labels if postprocess_labels is not None else (lambda reg_lab: reg_lab)
        
        if self.preset == 'single': # base_k = 200
            self.images = lambda rgb, hsv, lab, gray: torch.stack([hsv], dim = -4)
            self.segmentations = [cv2.ximgproc.segmentation.createGraphSegmentation(self.sigma, float(base_k), self.min_size)]
            self.strategies = torch.tensor([
                [0.25, 0.25, 0.25, 0.25],
            ])
            
        elif self.preset == 'fast':
            self.images = lambda rgb, hsv, lab, gray: torch.stack([hsv, lab], dim = -4)
            self.segmentations = [cv2.ximgproc.segmentation.createGraphSegmentation(self.sigma, float(k), self.min_size) for k in range(self.base_k, 1 + self.base_k + self.inc_k * 2, self.inc_k)]
            self.strategies = torch.tensor([
                [0.25, 0.25, 0.25, 0.25],
                [0.33, 0.33, 0.33, 0.00]
            ])
        
        elif self.preset == 'quality':
            self.images = lambda rgb, hsv, lab, gray: torch.stack([hsv, lab, gray.expand_as(hsv), hsv[..., :1, :, :].expand_as(hsv), torch.cat([rgb[..., :2, :, :],  gray], dim = -3)], dim = -4)
            self.segmentations = [cv2.ximgproc.segmentation.createGraphSegmentation(self.sigma, float(k), self.min_size) for k in range(self.base_k, 1 + self.base_k + self.inc_k * 4, self.inc_k)]
            self.strategies = torch.tensor([
                [0.25, 0.25, 0.25, 0.25],
                [0.33, 0.33, 0.33, 0.00],
                [1.00, 0.00, 0.00, 0.00],
                [0.00, 0.00, 1.00, 0.00]
            ])

    @staticmethod
    def get_region_mask(reg_lab, regs):
        return torch.stack([(reg_lab[reg['plane_id'][:-1]][..., None] == torch.tensor(list(reg['ids']), device = reg_lab.device, dtype = reg_lab.dtype)).any(dim = -1) for reg in regs])

    def forward(self, img : 'B3HW'):
        img = torch.as_tensor(img).contiguous()
        assert img.is_floating_point() and img.ndim == 4 and img.shape[-3] == 3

        hsv, lab, gray = rgb_to_hsv(img), rgb_to_lab(img), rgb_to_grayscale(img)
        hsv[..., 0, :, :] /= 2.0 * math.pi 
        hsv *= 255.0
        lab[..., 0, :, :] *= 255.0/100.0
        lab[..., 1:, :, :] += 128.0
        gray *= 255.0
        img *= 255.0
        
        imgs = self.images(img, hsv, lab, gray)

        reg_lab = torch.stack([torch.as_tensor(gs.processImage(img.movedim(-3, -1).numpy())) for img in imgs.flatten(end_dim = -4) for gs in self.segmentations]).unflatten(0, imgs.shape[:-3] + (len(self.segmentations),))
        reg_lab = self.postprocess_labels(reg_lab)
        num_segments = 1 + reg_lab.amax(dim = (-2, -1))
        max_num_segments = int(num_segments.amax())

        imgs_normalized_gaussian_grads = normalize_min_max(image_gaussian_grads(imgs.flatten(end_dim = -4)).unflatten(0, imgs.shape[:-3]), dim = (-2, -1))

        features = HandcraftedRegionFeatures(expand_dim(imgs, len(self.segmentations), dim = 2).flatten(end_dim = 2), expand_dim(imgs_normalized_gaussian_grads, len(self.segmentations), dim = 2).flatten(end_dim = 2), reg_lab.flatten(end_dim = 2), max_num_segments = max_num_segments)
        affinity = features.compute_region_affinity()
        features.expand_and_flatten(len(self.strategies))
        
        graphadj = self.build_graph(reg_lab, max_num_segments = max_num_segments).flatten(end_dim = -3)
        graphadj = (graphadj[..., None, None] * affinity.unsqueeze(-2) * self.strategies).sum(dim = -1)
        graphadj.masked_fill_(graphadj.isnan(), 0)

        regs = [dict(plane_id = (b, i, g, s), id = r1, plane_idx = plane_idx, level = 0 if r1 < int(num_segments[b, i, g]) else -1, bbox = tuple(features.xywh_[plane_idx * max_num_segments + r1]), strategy = strategy, ids = {r1}, parent_id = -1) for plane_idx, (b, i, g, s, strategy) in enumerate((b, i, g, s, strategy) for b in range(num_segments.shape[0]) for i in range(num_segments.shape[1]) for g in range(num_segments.shape[2]) for s, strategy in enumerate(self.strategies.tolist())) for r1 in range(graphadj.shape[-2])]
        
        PQ = []
        
        ga = graphadj.movedim(-1, -3).flatten(end_dim = -3)
        ga_sparse = ga.to_sparse().coalesce() # NOTE: coalesce call is to guard against sparse oddities https://github.com/pytorch/pytorch/issues/73479
        if ga_sparse._nnz() > 0:
            (plane_idx, r1, r2), sim = ga_sparse.indices(), ga_sparse.values()

            plane_idx *= max_num_segments
            PQ = list(zip(sim.neg().tolist(), (plane_idx + r1).tolist(), (plane_idx + r2).tolist()))
            gasym_sparse = (ga + ga.transpose(-2, -1)).to_sparse()
            (plane_idx, r1, r2), sim = gasym_sparse.indices(), gasym_sparse.values()
            plane_idx *= max_num_segments
            graph = {k : set(t[1] for t in g) for k, g in itertools.groupby(zip((plane_idx + r1).tolist(), (plane_idx + r2).tolist()), key = lambda t: t[0])}
        
        heapq.heapify(PQ)
        
        while PQ:
            negsim, u, v = heapq.heappop(PQ)
            if u not in graph or v not in graph:
                continue
            
            reg_fro, reg_to = regs[u], regs[v]
            regs.append(dict(reg_fro, level = 1 + max(reg_fro['level'], reg_to['level']), ids = reg_fro['ids'] | reg_to['ids'], id = min(reg_fro['id'], reg_to['id'])))
            reg_fro['parent_id'] = reg_to['parent_id'] = len(regs) - 1
            regs[-1]['bbox'] = features.merge_regions(reg_fro['id'], reg_to['id'], reg_fro['plane_idx'])

            for new_edge in self.contract_graph_edge(u, v, reg_fro['parent_id'], features, regs, graph):
                heapq.heappush(PQ, new_edge)
        
        for reg in regs:
            reg['rank'] = self.compute_region_rank(reg)
        key_img_id, key_rank = (lambda reg: reg['plane_id'][0]), (lambda reg: reg['rank'])
        by_image = {k: sorted(list(g), key = key_rank) for k, g in itertools.groupby(sorted([reg for reg in regs if reg['level'] >= 0], key = key_img_id), key = key_img_id)}
        without_duplicates = [{reg['bbox'] : i for i, reg in enumerate(by_image.get(b, []))} for b in range(len(img))]
        #TODO: add size filtering
        return [torch.as_tensor(list(without_duplicates[b].keys()), dtype = torch.int16) for b in range(len(img))], [[by_image[b][i] for i in without_duplicates[b].values()] for b in range(len(img))], reg_lab
    
    @staticmethod
    def build_graph(reg_lab : 'BIGHW', max_num_segments : int):
        I = torch.stack(torch.meshgrid(*[torch.arange(s) for s in reg_lab.shape[:3]]), dim = -1)[..., None, None]
        DX = torch.stack([reg_lab[..., :, :-1], reg_lab[..., :, 1:]], dim = -1).movedim(-1, -3)
        DY = torch.stack([reg_lab[..., :-1, :], reg_lab[..., 1:, :]], dim = -1).movedim(-1, -3)
        dx = torch.cat([I.expand(-1, -1, -1, -1, *DX.shape[-2:]), DX], dim = -3).flatten(start_dim = -2)
        dy = torch.cat([I.expand(-1, -1, -1, -1, *DY.shape[-2:]), DY], dim = -3).flatten(start_dim = -2)
        i = torch.cat([dx, dy], dim = -1).movedim(-2, 0).flatten(start_dim = 1)
        v = torch.ones(i.shape[1], dtype = torch.bool)
        A = torch.sparse_coo_tensor(i, v, reg_lab.shape[:-2] + (max_num_segments, max_num_segments), dtype = torch.int64)
        A += A.transpose(-1, -2)
        return A.coalesce().to(torch.bool).to_dense().triu(diagonal = 1)

    @staticmethod
    def contract_graph_edge(u, v, ww, features, regs, graph):
        graph[ww] = set()
        new_edges = []
        for uu in [u, v]:
            for vv in graph.pop(uu, []):
                if vv == u or vv == v:
                    continue

                new_edges.append((-features.compute_region_affinity(regs[ww]['id'], regs[vv]['id'], regs[ww]['plane_idx'], *regs[ww]['strategy']), ww, vv))

                graph[vv].remove(uu)
                graph[vv].add(ww)
                graph[ww].add(vv)
        return new_edges

class HandcraftedRegionFeatures:
    def __init__(self, imgs : 'B3HW', imgs_normalized_gaussian_grads : 'B23HW', reg_lab : 'BHW', max_num_segments: int, color_hist_bins = 32, texture_hist_bins = 8, neginf = torch.tensor(-32000, dtype = torch.int16), posinf = torch.tensor(32000, dtype = torch.int16), mini_batch_size = 128):
        img_channels, img_height, img_width = imgs.shape[-3:]
        self.img_size = img_height * img_width
        self.max_num_segments = max_num_segments
        
        Z = reg_lab.flatten(start_dim = -2).to(torch.int64)
        self.region_sizes = torch.zeros(reg_lab.shape[:-2] + (self.max_num_segments, ), dtype = torch.float32).scatter_add_(-1, Z, expand_ones_like(Z))
        
        Z = (reg_lab[..., None, :, :] * color_hist_bins + imgs.mul((color_hist_bins - 1) / 255.0)).flatten(start_dim = -2).to(torch.int64)
        self.color_hist = torch.zeros(reg_lab.shape[:-2] + (img_channels, self.max_num_segments * color_hist_bins), dtype = torch.float32).scatter_add_(-1, Z, expand_ones_like(Z)).unflatten(-1, (self.max_num_segments, color_hist_bins)).movedim(-2, -3).flatten(start_dim = -2).contiguous()
        self.color_hist /= self.color_hist.sum(dim = -1, keepdim = True)

        Z = (reg_lab[..., None, None, :, :] * texture_hist_bins + imgs_normalized_gaussian_grads.mul(texture_hist_bins - 1)).flatten(start_dim = -2).to(torch.int64)
        self.texture_hist = torch.zeros(reg_lab.shape[:-2] + (img_channels, imgs_normalized_gaussian_grads.shape[-3], self.max_num_segments * texture_hist_bins), dtype = torch.float32).scatter_add_(-1, Z, expand_ones_like(Z)).unflatten(-1, (self.max_num_segments, texture_hist_bins)).movedim(-2, -4).flatten(start_dim = -3).contiguous()
        self.texture_hist /= self.texture_hist.sum(dim = -1, keepdim = True)
        
        xywh_ = torch.tensor([[img_width, img_height, 0, 0]], dtype = torch.int64).repeat(reg_lab.shape[-3], max_num_segments, 1).flatten().tolist()
        reg_lab_ = reg_lab.flatten().tolist()
        k = 0
        for b in range(reg_lab.shape[-3]):
            for y in range(reg_lab.shape[-2]):
                for x in range(reg_lab.shape[-1]):
                    r = reg_lab_[k]
                    xywh_[b * 4 * max_num_segments + r * 4 + 0] = min(x, xywh_[b * 4 * max_num_segments + r * 4 + 0])
                    xywh_[b * 4 * max_num_segments + r * 4 + 1] = min(y, xywh_[b * 4 * max_num_segments + r * 4 + 1])
                    xywh_[b * 4 * max_num_segments + r * 4 + 2] = max(x, xywh_[b * 4 * max_num_segments + r * 4 + 2])
                    xywh_[b * 4 * max_num_segments + r * 4 + 3] = max(y, xywh_[b * 4 * max_num_segments + r * 4 + 3])
                    #xywh = self.xywh[b, reg_lab_[k]]
                    #xywh[0].clamp_(max = x)
                    #xywh[1].clamp_(max = y)
                    #xywh[2].clamp_(min = x)
                    #xywh[3].clamp_(min = y)
                    k += 1
        self.xywh = torch.as_tensor(xywh_, dtype = torch.int64).view(-1, max_num_segments, 4)
        self.xywh[..., 2] -= self.xywh[..., 0]
        self.xywh[..., 3] -= self.xywh[..., 1]
        
        #yx = torch.stack(torch.meshgrid(torch.arange(reg_lab.shape[-2], dtype = torch.int16), torch.arange(reg_lab.shape[-1], dtype = torch.int16)))
        #arange = torch.arange(self.max_num_segments)
        #self.xywh = []
        #for b in range(0, len(arange), mini_batch_size):
        #    arange_ = arange[b : b + mini_batch_size]
        #    mask = (reg_lab.unsqueeze(-1) == arange_).movedim(-1, -3).unsqueeze(-3)
        #    masked_min, masked_max = torch.where(mask, yx, posinf), torch.where(mask, yx, neginf)
        #    (ymin, xmin), (ymax, xmax) = masked_min.amin(dim = (-2, -1)).unbind(-1), masked_max.amax(dim = (-2, -1)).unbind(-1)
        #    self.xywh.append(torch.stack([xmin, ymin, xmax - xmin + 1, ymax - ymin + 1], dim = -1))
        #self.xywh = torch.cat(self.xywh, dim = -2)

        self.texture_hist_buffer = torch.empty(self.texture_hist.shape[-1], dtype = self.texture_hist.dtype)
        self.color_hist_buffer = torch.empty(self.color_hist.shape[-1], dtype = self.color_hist.dtype)
    
    def compute_region_affinity(self, r1 = None, r2 = None, plane_idx = None, fill = 0.0, texture = 0.0, size = 0.0, color = 0.0):
        bbox_size_tensor = lambda xywh: xywh[..., -2] * xywh[..., -1]
        bbox_size = lambda xywh: xywh[-2] * xywh[-1]
        clamp01 = lambda x: max(0, min(1, x))
        
        if r1 is None and r2 is None:
            size_affinity = (1 - (self.region_sizes.unsqueeze(-1) + self.region_sizes.unsqueeze(-2)).div_(self.img_size)).clamp_(min = 0, max = 1)
            fill_affinity = (1 - (bbox_size_tensor(bbox_merge_tensor(self.xywh.unsqueeze(-2), self.xywh.unsqueeze(-3))) - self.region_sizes.unsqueeze(-1) - self.region_sizes.unsqueeze(-2)) / self.img_size).clamp_(min = 0, max = 1)
            color_affinity = torch.min(self.color_hist.unsqueeze(-2), self.color_hist.unsqueeze(-3)).sum(dim = -1)
            texture_affinity = torch.min(self.texture_hist.unsqueeze(-2), self.texture_hist.unsqueeze(-3)).sum(dim = -1)
            return torch.stack([fill_affinity, texture_affinity, size_affinity, color_affinity], dim = -1)

        else:
            plane_idx *= self.max_num_segments
            
            res = 0.0
            
            if size > 0:
                res += size * clamp01(1 - (self.region_sizes_[plane_idx + r1] + self.region_sizes_[plane_idx + r2]) / self.img_size)
            
            if fill > 0:
                res += fill * clamp01(1 - (bbox_size(bbox_merge(self.xywh_[plane_idx + r1], self.xywh_[plane_idx + r2])) - self.region_sizes_[plane_idx + r1] - self.region_sizes_[plane_idx + r2]) / self.img_size)
            
            if color > 0:
                res += color * float(torch.min(self.color_hist_[plane_idx + r1], self.color_hist_[plane_idx + r2], out = self.color_hist_buffer).sum(dim = -1))
            
            if texture > 0:
                res += texture * float(torch.min(self.texture_hist_[plane_idx + r1], self.texture_hist_[plane_idx + r2], out = self.texture_hist_buffer).sum(dim = -1))

            return res
    
    def merge_regions(self, r1, r2, plane_idx):
        plane_idx *= self.max_num_segments
        
        s1, s2 = self.region_sizes_[plane_idx + r1], self.region_sizes_[plane_idx + r2]
        s1s2 = s1 + s2

        self.region_sizes_[plane_idx + r2] = self.region_sizes_[plane_idx + r1] = s1s2

        self.xywh_[plane_idx + r2] = self.xywh_[plane_idx + r1] = bbox_merge(self.xywh_[plane_idx + r1], self.xywh_[plane_idx + r2])
        
        self.color_hist_[plane_idx + r2].copy_(self.color_hist_[plane_idx + r1].mul_(s1).add_(self.color_hist_[plane_idx + r2].mul_(s2)).div_(s1s2))
        
        self.texture_hist_[plane_idx + r2].copy_(self.texture_hist_[plane_idx + r1].mul_(s1).add_(self.texture_hist_[plane_idx + r2].mul_(s2)).div_(s1s2))
        return self.xywh_[plane_idx + r2]

        
    def expand_and_flatten(self, num_strategies):
        self.region_sizes_ = expand_dim(self.region_sizes, num_strategies, dim = -2).flatten().tolist()
        self.xywh_ = expand_dim(self.xywh, num_strategies, dim = -3).flatten(end_dim = -2).tolist()
        self.color_hist_ = expand_dim(self.color_hist, num_strategies, dim = -3).flatten(end_dim = -2)
        self.texture_hist_ = expand_dim(self.texture_hist, num_strategies, dim = -3).flatten(end_dim = -2)
