from __future__ import print_function
import os
import pickle

import time
import logging
import numpy
from src.data import get_test_loader
import time
import numpy as np
from collections import OrderedDict
# from train import max_length
from tqdm import tqdm
from mindspore import ops
import mindspore as ms
import copy
from ipdb import set_trace

class AverageMeter(object):
    """Computes and stores the average and current value"""

    def __init__(self):
        self.reset()

    def reset(self):
        self.val = 0
        self.avg = 0
        self.sum = 0
        self.count = 0

    def update(self, val, n=0):
        self.val = val
        self.sum += val * n
        self.count += n
        self.avg = self.sum / (.0001 + self.count)

    def __str__(self):
        """String representation for logging
        """
        # for values that should be recorded exactly e.g. iteration number
        if self.count == 0:
            return str(self.val)
        # for stats
        return '%.4f (%.4f)' % (self.val, self.avg)


class LogCollector(object):
    """A collection of logging objects that can change from train to val"""

    def __init__(self):
        # to keep the order of logged variables deterministic
        self.meters = OrderedDict()

    def update(self, k, v, n=0):
        # create a new meter if previously not recorded
        if k not in self.meters:
            self.meters[k] = AverageMeter()
        self.meters[k].update(v, n)

    def __str__(self):
        """Concatenate the meters in one log line
        """
        s = ''
        for i, (k, v) in enumerate(self.meters.items()): # drop iter
            if i > 0:
                s += '  '
            s += k + ' ' + str(v)
        return s

    def tb_log(self, tb_logger, prefix='', step=None):
        """Log using tensorboard
        """
        for k, v in self.meters.items(): # drop iter
            tb_logger.log_value(prefix + k, v.val, step=step)



# def encode_data(model, data_loader, log_step=10, logging=print):
def encode_data(model, 
                data_loader, 
                log_step=10, 
                logging=print,
                val_dataset_len=None, 
                opt=None):
    """Encode all images and captions loadable by `data_loader`
    """

    batch_time = AverageMeter()
    val_logger = LogCollector()

    # switch to evaluate mode
    model.set_train(False)

    end = time.time()

    # numpy array to keep all the embeddings
    img_embs = None
    cap_embs = None
    cap_masks = None
#     print("start loading val data...")
    ExpandDims = ms.ops.ExpandDims()
    for i, (images, captions, lengths, ids, img_ids) in enumerate(data_loader):
        """
        images     (128, 36, 2048)
        captions   (128, 49)
        lengths    (128, 1)
        ids        (128, 1)
        img_ids    (128, 1)
        """
        if i==0: batch=images.shape[0]

        lengths = ops.Squeeze()(lengths)
        ids = ops.Squeeze()(ids).asnumpy()

        l_list = [int(i) for i in lengths.asnumpy().tolist()]
        mask_list = [ExpandDims(ms.Tensor(i * [0] + (opt.max_length + 3 - i) * [1], ms.int32), 0) for i in l_list]
        mask = ops.Concat(0)(mask_list)

        """
        images     (128, 36, 2048)
        captions   (128, 87) 
        lengths    (128)
        mask       (128, 87)
        """

        img_emb, cap_emb = model(images, captions, lengths, mask)
        # img_emb   (128, 36, 1024)
        # cap_emb   (128, 87, 1024)
        if img_embs is None:
            if opt.cross_model:
                img_embs = np.zeros((val_dataset_len, img_emb.shape[1], img_emb.shape[2]))
                cap_embs = np.zeros((val_dataset_len, opt.max_length + 3, cap_emb.shape[2]))
                cap_masks = np.zeros((val_dataset_len, opt.max_length + 3), dtype=int)
            else:
                img_embs = np.zeros((val_dataset_len, img_emb.shape[1]))
                if model.opt.measure == "attention":
                    cap_embs = np.zeros((val_dataset_len, cap_emb.shape[1]))
                else:
                    cap_embs = np.zeros((val_dataset_len, cap_emb.shape[1]))

        # preserve the embeddings by copying from gpu and converting to numpy
        img_embs[ids] = copy.deepcopy(img_emb.asnumpy())
        cap_embs[ids] = copy.deepcopy(cap_emb.asnumpy())
        lengths = lengths.asnumpy().tolist()
        l_list = [int(l_now) for l_now in lengths]
        cur_mask = np.ones((len(lengths), opt.max_length + 3), dtype=int)   # [128, 87]
        for mask_idx, mask_l in enumerate(l_list):
            cur_mask[mask_idx, :mask_l] = 0

        cap_masks[ids] = cur_mask
        # measure accuracy and record loss
        # model.forward_loss(img_emb, cap_emb)
        # measure elapsed time
        batch_time.update(time.time() - end)
        end = time.time()

        if i % log_step == 0:
            logging('Test: [{0}/{1}]\t'
                    '{e_log}\t'
                    'Time {batch_time.val:.3f} ({batch_time.avg:.3f})\t'
                .format(
                i*batch, val_dataset_len, batch_time=batch_time,
                e_log="Unavailable"))
        del images, captions
#     set_trace()
    return img_embs, cap_embs, cap_masks
    """
    img_embs     (200, 36, 1024)
    cap_embs     (200, 87, 1024)
    cap_masks    (200, 87)
    """



def i2t(images, captions, masks, npts=None, measure='cosine', return_ranks=False,
        criterion=None, opt=None):
    """
    Images->Text (Image Annotation)       (5000, 36, 1024)
    Images: (5N, K) matrix of images      (5000, 50, 1024)
    Captions: (5N, K) matrix of captions
    masks    (5000, 50)
    measure   'gate_fusion_new'
    return_ranks   False
    """
    criterion.set_train(False)
    if npts is None:
        npts = images.shape[0] // 5
    index_list = []
    gv1_list = []
    gv2_list = []

    ranks = numpy.zeros(npts)
    top1 = numpy.zeros(npts)
    # set_trace()
    score_matrix = numpy.zeros((images.shape[0] // 5, captions.shape[0]))

    for index in tqdm(range(npts)):

        # Get query image
        if opt.cross_model:
            im = images[5 * index].reshape(1, images.shape[1], images.shape[2])
        else:
            im = images[5 * index].reshape(1, images.shape[1])
        # set_trace()
        # Compute scores
        if 'gate_fusion' in measure:  # 执行这个
            bs = 5
            if index % bs == 0:
                if index % 5 == 0:
                    print('[' + str(index) + '/' + str(npts) + ']')
                mx = min(images.shape[0], 5 * (index + bs))
                im2 = images[5 * index:mx:5]  # (5, 36, 1024)
                tt1 = time.time()

                duan_ge = 200
                d2 = []
                for i in range(captions.shape[0] // duan_ge):
                    low = i * duan_ge
                    high = low + duan_ge
                    captions_ = captions[low:high, :, :]
                    masks_ = masks[low:high, :]
                    d2_ = criterion(ms.Tensor(im2, ms.float32),  # (5, 50)
                                      ms.Tensor(captions_, ms.float32),
                                      True, 
                                      keep=True, #"regions",  
                                      mask=ms.Tensor(masks_, ms.int32))
                    d2.append(d2_.asnumpy())
                tt2 = time.time()
                d2 = np.concatenate(d2, -1)
            d = d2[index % bs]
        inds = numpy.argsort(d)[::-1]
        index_list.append(inds[0])
        score_matrix[index] = d

        # Score
        rank = 1e20
        for i in range(5 * index, 5 * index + 5, 1):
            tmp = numpy.where(inds == i)[0][0]
            if tmp < rank:
                rank = tmp
        ranks[index] = rank
        top1[index] = inds[0]

    # i2t
    stat_num = 0
    minnum_rank_image = np.array([1e7] * npts)
    for i in range(npts):
        cur_rank = np.argsort(score_matrix[i])[::-1]
        for index, j in enumerate(cur_rank):
            if j in range(5 * i, 5 * i + 5):
                stat_num += 1
                minnum_rank_image[i] = index
                break
    print("i2t stat num:", stat_num)

    i2t_r1 = 100.0 * len(numpy.where(minnum_rank_image < 1)[0]) / len(minnum_rank_image)
    i2t_r5 = 100.0 * len(numpy.where(minnum_rank_image < 5)[0]) / len(minnum_rank_image)
    i2t_r10 = 100.0 * len(numpy.where(minnum_rank_image < 10)[0]) / len(minnum_rank_image)
    i2t_medr = numpy.floor(numpy.median(minnum_rank_image)) + 1
    i2t_meanr = minnum_rank_image.mean() + 1

    stat_num = 0
    score_matrix = score_matrix.transpose()
    minnum_rank_caption = np.array([1e7] * npts * 5)
    for i in range(5 * npts):
        img_id = i // 5
        cur_rank = np.argsort(score_matrix[i])[::-1]
        for index, j in enumerate(cur_rank):
            if j == img_id:
                stat_num += 1
                minnum_rank_caption[i] = index
                break

    print("t2i stat num:", stat_num)

    t2i_r1 = 100.0 * len(numpy.where(minnum_rank_caption < 1)[0]) / len(minnum_rank_caption)
    t2i_r5 = 100.0 * len(numpy.where(minnum_rank_caption < 5)[0]) / len(minnum_rank_caption)
    t2i_r10 = 100.0 * len(numpy.where(minnum_rank_caption < 10)[0]) / len(minnum_rank_caption)
    t2i_medr = numpy.floor(numpy.median(minnum_rank_caption)) + 1
    t2i_meanr = minnum_rank_caption.mean() + 1

    # print("t2i results:", t2i_r1, t2i_r5, t2i_r10, t2i_medr, t2i_meanr)

    # Compute metrics
    r1 = 100.0 * len(numpy.where(ranks < 1)[0]) / len(ranks)
    r5 = 100.0 * len(numpy.where(ranks < 5)[0]) / len(ranks)
    r10 = 100.0 * len(numpy.where(ranks < 10)[0]) / len(ranks)
    medr = numpy.floor(numpy.median(ranks)) + 1
    meanr = ranks.mean() + 1
    if return_ranks:
        return (i2t_r1, i2t_r5, i2t_r10, i2t_medr, i2t_meanr), (
        t2i_r1, t2i_r5, t2i_r10, t2i_medr, t2i_meanr), score_matrix
    else:
        return (i2t_r1, i2t_r5, i2t_r10, i2t_medr, i2t_meanr), (t2i_r1, t2i_r5, t2i_r10, t2i_medr, t2i_meanr)



def evalrank(model_path, data_path=None, split='dev', fold5=False, return_ranks=False):
    """
    Evaluate a trained model on either dev or test. If `fold5=True`, 5 fold
    cross-validation is done (only for MSCOCO). Otherwise, the full data is
    used for evaluation.
    """
    # load model and options
    logging.basicConfig(format='%(asctime)s %(message)s', level=logging.INFO)
    checkpoint = torch.load(model_path)
    opt = checkpoint['opt']
    if data_path is not None:
        opt.data_path = data_path
    # load vocabulary used by the model

    vocab = pickle.load(open(os.path.join(
        opt.vocab_path, '%s_vocab.pkl' % opt.data_name), 'rb'))
    opt.vocab_size = len(vocab)
    opt.distributed = False
    opt.use_all = True
    opt.instance_loss = False
    opt.attention = False

    print(opt)
    # construct model
    model = VSE(opt)

    if "cnn.classifier.1.weight" in checkpoint['model'][0]:
        checkpoint['model'][0]["cnn.classifier.0.weight"] = checkpoint['model'][0].pop("cnn.classifier.1.weight")
        checkpoint['model'][0]["cnn.classifier.0.bias"] = checkpoint['model'][0].pop("cnn.classifier.1.bias")
        checkpoint['model'][0]["cnn.classifier.3.weight"] = checkpoint['model'][0].pop("cnn.classifier.4.weight")
        checkpoint['model'][0]["cnn.classifier.3.bias"] = checkpoint['model'][0].pop("cnn.classifier.4.bias")

    # load model state
    model.load_state_dict(checkpoint['model'])

    print('Loading dataset')
    data_loader = get_test_loader(split, opt.data_name, vocab, opt.crop_size,
                                  opt.batch_size, opt.workers, opt)

    print('Computing results...')
    img_embs, cap_embs = encode_data(model, data_loader)
    print('Images: %d, Captions: %d' %
          (img_embs.shape[0] / 5, cap_embs.shape[0]))

    if not fold5:
        # no cross-validation, full evaluation
        r, rt = i2t(img_embs, cap_embs, measure=opt.measure, return_ranks=True)
        ri, rti = t2i(img_embs, cap_embs,
                      measure=opt.measure, return_ranks=True)
        ar = (r[0] + r[1] + r[2]) / 3
        ari = (ri[0] + ri[1] + ri[2]) / 3
        rsum = r[0] + r[1] + r[2] + ri[0] + ri[1] + ri[2]
        print("rsum: %.1f" % rsum)
        print("Average i2t Recall: %.1f" % ar)
        print("Image to text: %.1f %.1f %.1f %.1f %.1f" % r)
        print("Average t2i Recall: %.1f" % ari)
        print("Text to image: %.1f %.1f %.1f %.1f %.1f" % ri)
    else:
        # 5fold cross-validation, only for MSCOCO
        results = []
        for i in range(5):
            r, rt0 = i2t(img_embs[i * 5000:(i + 1) * 5000],
                         cap_embs[i * 5000:(i + 1) *
                                  5000], measure=opt.measure,
                         return_ranks=True)
            print("Image to text: %.1f, %.1f, %.1f, %.1f, %.1f" % r)
            ri, rti0 = t2i(img_embs[i * 5000:(i + 1) * 5000],
                           cap_embs[i * 5000:(i + 1) *
                                    5000], measure=opt.measure,
                           return_ranks=True)
            if i == 0:
                rt, rti = rt0, rti0
            print("Text to image: %.1f, %.1f, %.1f, %.1f, %.1f" % ri)
            ar = (r[0] + r[1] + r[2]) / 3
            ari = (ri[0] + ri[1] + ri[2]) / 3
            rsum = r[0] + r[1] + r[2] + ri[0] + ri[1] + ri[2]
            print("rsum: %.1f ar: %.1f ari: %.1f" % (rsum, ar, ari))
            results += [list(r) + list(ri) + [ar, ari, rsum]]

        print("-----------------------------------")
        print("Mean metrics: ")
        mean_metrics = tuple(np.array(results).mean(axis=0).flatten())
        print("rsum: %.1f" % (mean_metrics[10] * 6))
        print("Average i2t Recall: %.1f" % mean_metrics[11])
        print("Image to text: %.1f %.1f %.1f %.1f %.1f" %
              mean_metrics[:5])
        print("Average t2i Recall: %.1f" % mean_metrics[12])
        print("Text to image: %.1f %.1f %.1f %.1f %.1f" %
              mean_metrics[5:10])
    if return_ranks:
        return rt, rti