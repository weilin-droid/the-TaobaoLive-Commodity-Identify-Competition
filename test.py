import os
import torch
import numpy as np
# from sklearn.metrics.pairwise import cosine_similarity
from torchvision import transforms
from torch.utils.data import DataLoader
from dataset import TestImageDataset, TestVideoDataset, TestDataset
from utils import Resizer_Test, Normalizer_Test, collater_test
from efficientdet.efficientdet import EfficientDet
from arcface.resnet import ResNet
from arcface.googlenet import GoogLeNet
from arcface.inception_v4 import InceptionV4
from arcface.inceptionresnet_v2 import InceptionResNetV2
from arcface.densenet import DenseNet
from arcface.resnet_cbam import ResNetCBAM
from config import get_args_efficientdet, get_args_arcface
from joint_bayesian.JointBayesian import verify
from tqdm import tqdm
import json

def cosine_similarity(a, b):
    return a@b.T

def pre_efficient(dataset, model, opt_e, cls_k, ins_f=True):
    params = {"batch_size": opt_e.batch_size,
                    "shuffle": False,
                    "drop_last": False,
                    "collate_fn": collater_test,
                    "num_workers": opt_e.workers}
    loader = DataLoader(dataset, **params)
    progress_bar = tqdm(loader)
    items = []
    for i, data in enumerate(progress_bar):
        scale = data['scale']
        with torch.no_grad():
            output_list = model([data['img'].cuda().float(), data['text'].cuda()])
        for j, output in enumerate(output_list):
            imgPath, imgID, frame = dataset.getImageInfo(i*opt_e.batch_size+j)
            scores, labels, instances, all_labels, boxes = output
            if cls_k:
                argmax = np.argpartition(all_labels.cpu(), kth=-cls_k, axis=1)[:, -cls_k:]
            else:
                argmax = -np.ones([len(all_labels), 1])
            boxes /= scale[j]
            for box_id in range(boxes.shape[0]):
                if instances[box_id, 0] == 0 and ins_f:
                    continue
                pred_prob = float(scores[box_id])
                if pred_prob < opt_e.cls_threshold:
                    break
                xmin, ymin, xmax, ymax = boxes[box_id, :]
                l = [frame, imgID, imgPath, int(xmin), int(ymin), int(xmax), int(ymax), argmax[box_id].tolist(), data['text'][j]]
                items.append(l)
    return items

def pre_bockbone(dataset, model, opt_a):
    params = {"batch_size": opt_a.batch_size,
                "shuffle": False,
                "drop_last": False,
                "num_workers": opt_a.workers}
    
    generator = DataLoader(dataset, **params)

    length = len(dataset)
    features_arr = np.zeros((length, opt_a.embedding_size))
    boxes_arr = np.zeros((length, 4))
    IDs = []
    frames = []
    classes = []

    progress_bar = tqdm(generator)
    for i, data in enumerate(progress_bar):
        img = data['img'].cuda()
        imgID = data['imgID']
        frame = data['frame']
        box = data['box'].numpy()
        cs = [d.view(-1, 1) for d in data['classes']]
        cs = torch.cat(cs, dim=1).tolist()
        text = data['text'].cuda()
        
        with torch.no_grad():
            features = model(img, text).cpu().numpy()
        classes += cs
        IDs += imgID
        frames += frame
        features_arr[i*opt_a.batch_size:min((i+1)*opt_a.batch_size, length), :] = features
        boxes_arr[i*opt_a.batch_size:min((i+1)*opt_a.batch_size, length), :] = box
        # features_arr = np.append(features_arr, features, axis=0)
        # boxes_arr = np.append(boxes_arr, box, axis=0)

    return features_arr, boxes_arr, IDs, frames, classes

def cal_cosine_similarity(vdo_features, img_features, vdo_IDs, img_IDs, k):
    vdo2img = []
    # length = vdo_features.shape[0]
    length_v = vdo_features.shape[0] // 2
    length_i = img_features.shape[0] // 2
    # cos = np.zeros((length_v, length_i))
    print('calculating cosine similarity...')
    for index in tqdm(range(1+(length_v-1)//1000)):
        if index < length_v//1000:
            cos_1 = cosine_similarity(vdo_features[1000*index:1000*(index+1)], img_features)
            cos_2 = cosine_similarity(vdo_features[length_v+1000*index:length_v+1000*(index+1)], img_features)
            cos = np.max(
                (cos_1[:, :length_i], cos_1[:, length_i:], cos_2[:, :length_i], cos_2[:, length_i:]), axis=0)     
        else:
            cos_1 = cosine_similarity(vdo_features[1000*index:length_v], img_features)
            cos_2 = cosine_similarity(vdo_features[length_v+1000*index:], img_features)
            cos = np.max(
                (cos_1[:, :length_i], cos_1[:, length_i:], cos_2[:, :length_i], cos_2[:, length_i:]), axis=0)
    
        # return cos
        
        # cos = np.max((cos[:length_v, :], cos[length_v:, :]), axis=0)

        argmax = np.argpartition(cos, kth=-k, axis=1)[:, -k:]
        
        for i in range(argmax.shape[0]):
            # d = {}
            for am in argmax[i, :]:
                if cos[i, am] > 0:
                    vdo2img.append([vdo_IDs[i+1000*index], img_IDs[am], cos[i, am], i+1000*index, am])
                #         if img_IDs[am] not in d:
                #             d[img_IDs[am]] = [cos[i, am], cos[i, am], am]
                #         else:
                #             l = d[img_IDs[am]][:]
                #             if cos[i, am] > l[1]:
                #                 l[1] = cos[i, am]
                #                 l[2] = am
                #             l[0] += cos[i, am]
                #             d[img_IDs[am]] = l
                # if len(d) == 0:
                #     continue
                # d = sorted(d.items(), key=lambda x:x[1][0], reverse=True)
                # vdo2img.append([vdo_IDs[i+1000*index], d[0][0], d[0][1][0], i+1000*index, d[0][1][2]])
                    # vdo_id, img_id, score, vdo_index, img_index
    return vdo2img

def joint_bayesian(opt, vdo_features, img_features, vdo_IDs, img_IDs, k):
    print('Calculating Joint Bayesian...')
    G = np.load(os.path.join(opt.saved_path, 'G.npy'))
    A = np.load(os.path.join(opt.saved_path, 'A.npy'))

    vdo2img = []
    length = vdo_features.shape[0]
    for index in tqdm(range(1+(length-1)//1000)):
        if index < length//1000:
            scores = verify(A, G, vdo_features[1000*index:1000*(index+1)], img_features)
        else:
            scores = verify(A, G, vdo_features[1000*index:], img_features)
        
        argmax = np.argpartition(scores, kth=-k, axis=1)[:, -k:]
        
        for i in range(argmax.shape[0]):
            for am in argmax[i, :]:
                if scores[i, am] > 0:
                    vdo2img.append([vdo_IDs[i+1000*index], img_IDs[am], scores[i, am], i+1000*index, am])
    return vdo2img

def createVdo2Img(imgs, vdos, k, opt_a):
    vdo2img = []
    config_list = opt_a.validation_config

    img_features_list = []
    vdo_features_list = []
    for network, size in config_list:
        opt_a.network = network
        opt_a.size = size

        if opt_a.network == 'resnet':
            backbone = ResNet(opt_a)
            b_name = opt_a.network+'_'+opt_a.mode+'_{}'.format(opt_a.num_layers_r)
        elif opt_a.network == 'googlenet':
            backbone = GoogLeNet(opt_a)
            b_name = opt_a.network
        elif opt_a.network == 'inceptionv4':
            backbone = InceptionV4(opt_a)
            b_name = opt_a.network
        elif opt_a.network == 'inceptionresnetv2':
            backbone = InceptionResNetV2(opt_a)
            b_name = opt_a.network
        elif opt_a.network == 'densenet':
            backbone = DenseNet(opt_a)
            b_name = opt_a.network+'_{}'.format(opt_a.num_layers_d)
        elif opt_a.network == 'resnet_cbam':
            backbone = ResNetCBAM(opt_a)
            b_name = opt_a.network+'_{}'.format(opt_a.num_layers_c)
        else:
            raise RuntimeError('Cannot Find the Model: {}'.format(opt_a.network))

        backbone.load_state_dict(torch.load(os.path.join(opt_a.saved_path, b_name+'.pth')))
        backbone.cuda()
        backbone.eval()

        dataset_det_img = TestDataset(opt_a.data_path, imgs, (opt_a.size, opt_a.size), mode='image')
        dataset_det_vdo = TestDataset(opt_a.data_path, vdos, (opt_a.size, opt_a.size), mode='video')

        print('creating features...')
        img_features, img_boxes, img_IDs, img_frames, img_classes = pre_bockbone(dataset_det_img, backbone, opt_a)
        vdo_features, vdo_boxes, vdo_IDs, vdo_frames, vdo_classes = pre_bockbone(dataset_det_vdo, backbone, opt_a)

        img_features_list.append(img_features)
        vdo_features_list.append(vdo_features)

        assert len(set([
            len(img_features), 
            len(img_boxes), 
            len(img_IDs), 
            len(img_frames), 
            len(img_classes)]))==1
        assert len(set([
            len(vdo_features), 
            len(vdo_boxes), 
            len(vdo_IDs), 
            len(vdo_frames), 
            len(vdo_classes)]))==1

        # cos_ = cal_cosine_similarity(vdo_features, img_features, vdo_IDs, img_IDs, k)

        # cos += cos_
    
    length_v = vdo_features.shape[0] // 2
    length_i = img_features.shape[0] // 2
    # cos = np.zeros((length_v, length_i))
    print('calculating cosine similarity...')
    for index in tqdm(range(1+(length_v-1)//1000)):
        cos_1 = 0
        cos_2 = 0
        for i in range(len(img_features_list)):
            if index < length_v//1000:
                cos_1 += cosine_similarity(vdo_features_list[i][1000*index:1000*(index+1)], img_features_list[i])
                cos_2 += cosine_similarity(vdo_features_list[i][length_v+1000*index:length_v+1000*(index+1)], img_features_list[i])     
            else:
                cos_1 += cosine_similarity(vdo_features_list[i][1000*index:length_v], img_features_list[i])
                cos_2 += cosine_similarity(vdo_features_list[i][length_v+1000*index:], img_features_list[i])

        cos = np.max(
                    (cos_1[:, :length_i], cos_1[:, length_i:], cos_2[:, :length_i], cos_2[:, length_i:]), axis=0)

    # length_v = vdo_features.shape[0] // 2
    # length_i = img_features.shape[0] // 2
    # cos = np.max((cos[:length_v, :length_i], cos[length_v:, length_i:], cos[length_v:, :length_i], cos[:length_v, length_i:]), axis=0)

        argmax = np.argpartition(cos, kth=-k, axis=1)[:, -k:]
        
        for i in range(argmax.shape[0]):
            for am in argmax[i, :]:
                if cos[i, am] > 0:
                    vdo2img.append([vdo_IDs[i+1000*index], img_IDs[am], cos[i, am], i+1000*index, am])

    return vdo2img, img_features_list[:length_i], vdo_features_list[:length_v], img_boxes[:length_i], vdo_boxes[:length_v], img_IDs[:length_i], vdo_IDs[:length_v], img_frames[:length_i], vdo_frames[:length_v], img_classes[:length_i], vdo_classes[:length_v]

def test(opt_a, opt_e):
    k = 2
    cls_k = 3

    dataset_img = TestImageDataset(
        root_dir=opt_e.data_path,
        dir_list=['validation_dataset_part1', 'validation_dataset_part2'],
        transform=transforms.Compose([Normalizer_Test(), Resizer_Test()]))
    dataset_vdo = TestVideoDataset(
        root_dir=opt_e.data_path,
        dir_list=['validation_dataset_part1', 'validation_dataset_part2'],
        transform=transforms.Compose([Normalizer_Test(), Resizer_Test()]))
        
    opt_e.num_classes = dataset_img.num_classes
    opt_e.vocab_size = dataset_img.vocab_size
    
    opt_e.imgORvdo = 'image'
    efficientdet_image = EfficientDet(opt_e)
    opt_e.imgORvdo = 'video'
    efficientdet_video = EfficientDet(opt_e)
    
    efficientdet_image.load_state_dict(torch.load(os.path.join(opt_e.saved_path, opt_e.network+'_image'+'.pth')))
    efficientdet_video.load_state_dict(torch.load(os.path.join(opt_e.saved_path, opt_e.network+'_video'+'.pth')))
    
    efficientdet_image.cuda()
    efficientdet_video.cuda()

    efficientdet_image.set_is_training(False)
    efficientdet_video.set_is_training(False)

    efficientdet_image.eval()
    efficientdet_video.eval()

    print('predicting boxs...')
    imgs = pre_efficient(dataset_img, efficientdet_image, opt_e, cls_k, ins_f=True)
    vdos = pre_efficient(dataset_vdo, efficientdet_video, opt_e, cls_k, ins_f=True)

    vdo2img, img_features_list, vdo_features_list, img_boxes, vdo_boxes, img_IDs, vdo_IDs, img_frames, vdo_frames, img_classes, vdo_classes = createVdo2Img(imgs, vdos, k, opt_a)

    # if opt_a.network == 'resnet':
    #     backbone = ResNet(opt_a)
    #     b_name = opt_a.network+'_'+opt_a.mode+'_{}'.format(opt_a.num_layers_r)
    # elif opt_a.network == 'googlenet':
    #     backbone = GoogLeNet(opt_a)
    #     b_name = opt_a.network
    # elif opt_a.network == 'inceptionv4':
    #     backbone = InceptionV4(opt_a)
    #     b_name = opt_a.network
    # elif opt_a.network == 'inceptionresnetv2':
    #     backbone = InceptionResNetV2(opt_a)
    #     b_name = opt_a.network
    # elif opt_a.network == 'densenet':
    #     backbone = DenseNet(opt_a)
    #     b_name = opt_a.network+'_{}'.format(opt_a.num_layers_d)
    # elif opt_a.network == 'resnet_cbam':
    #     backbone = ResNetCBAM(opt_a)
    #     b_name = opt_a.network+'_{}'.format(opt_a.num_layers_c)
    # else:
    #     raise RuntimeError('Cannot Find the Model: {}'.format(opt_a.network))

    # backbone.load_state_dict(torch.load(os.path.join(opt_a.saved_path, b_name+'.pth')))
    # backbone.cuda()
    # backbone.eval()

    # dataset_det_img = TestDataset(opt_a.data_path, imgs, (opt_a.size, opt_a.size), mode='image')
    # dataset_det_vdo = TestDataset(opt_a.data_path, vdos, (opt_a.size, opt_a.size), mode='video')

    # print('creating features...')
    # img_features, img_boxes, img_IDs, img_frames, img_classes = pre_bockbone(dataset_det_img, backbone, opt_a)
    # vdo_features, vdo_boxes, vdo_IDs, vdo_frames, vdo_classes = pre_bockbone(dataset_det_vdo, backbone, opt_a)

    # assert len(set([
    #     len(img_features), 
    #     len(img_boxes), 
    #     len(img_IDs), 
    #     len(img_frames), 
    #     len(img_classes)]))==1
    # assert len(set([
    #     len(vdo_features), 
    #     len(vdo_boxes), 
    #     len(vdo_IDs), 
    #     len(vdo_frames), 
    #     len(vdo_classes)]))==1

    # vdo2img = cal_cosine_similarity(vdo_features, img_features, vdo_IDs, img_IDs, k)

    vdo2img_d = {}
    print('merging videos...')
    for l in tqdm(vdo2img):
        if cls_k != 0:
            flag = False
            vdo_index = l[3]
            img_index = l[4]
            vdo_cls = vdo_classes[vdo_index]
            img_cls = img_classes[img_index]
            if len(set(vdo_cls) & set(img_cls)) == 0:
                continue
        if l[0] not in vdo2img_d:
            vdo2img_d[l[0]] = {}
        if l[1] not in vdo2img_d[l[0]]:
            vdo2img_d[l[0]][l[1]] = [l[2], l[2], l[3], l[4]]
        else:
            lst = vdo2img_d[l[0]][l[1]]
            if l[2] > lst[1]:
                lst[1] = l[2]
                lst[2] = l[3]
                lst[3] = l[4]
            lst[0] += l[2]
            vdo2img_d[l[0]][l[1]] = lst

    vdo_dic = {}
    for i in range(len(vdo_IDs)):
        if vdo_IDs[i] not in vdo_dic:
            vdo_dic[vdo_IDs[i]] = {}
        if vdo_frames[i] not in vdo_dic[vdo_IDs[i]]:
            vdo_dic[vdo_IDs[i]][vdo_frames[i]] = []
        vdo_dic[vdo_IDs[i]][vdo_frames[i]].append(i)
    
    img_dic = {}
    for i in range(len(img_IDs)):
        if img_IDs[i] not in img_dic:
            img_dic[img_IDs[i]] = []
        img_dic[img_IDs[i]].append(i)
    
    result = {}
    print('testing...')
    for k in tqdm(vdo2img_d.keys()):
        max_pre = sorted(vdo2img_d[k].items(), key=lambda x:x[1][0], reverse=True)[0]
        vdo_id = vdo_IDs[max_pre[1][2]]
        img_id = img_IDs[max_pre[1][3]]
        frame_index = vdo_frames[max_pre[1][2]]
        vdo_index = vdo_dic[vdo_id][frame_index]
        img_index = img_dic[img_id]
        result[vdo_id] = {}
        result[vdo_id]['item_id'] = img_id
        result[vdo_id]['frame_index'] = int(frame_index)
        result[vdo_id]['result'] = []
        cos = 0
        for i in range(len(img_features_list)):
            vdo_f = np.zeros((0, opt_a.embedding_size))
            for index in vdo_index:
                vdo_f = np.append(vdo_f, vdo_features_list[i][index].reshape(1, opt_a.embedding_size), axis=0)
            img_f = np.zeros((0, opt_a.embedding_size))
            for index in img_index:
                img_f = np.append(img_f, img_features_list[i][index].reshape(1, opt_a.embedding_size), axis=0)
            cos += cosine_similarity(vdo_f, img_f)
        for i, index in enumerate(vdo_index):
            simis = [cos[i, j] for j in range(len(img_index))]
            simis_is = np.argsort(-np.array(simis))[:3]
            for simis_i in simis_is:
                if simis[simis_i] < 0.2:
                    break
                img_i = img_index[simis_i]
                d = {}
                d['img_name'] = img_frames[img_i]
                d['item_box'] = list(map(int, img_boxes[img_i].tolist()))
                d['frame_box'] = list(map(int, vdo_boxes[index].tolist()))
                # d['cos'] = simis[simis_i]
                result[vdo_id]['result'].append(d)

        if len(result[vdo_id]['result']) == 0:
            del result[vdo_id]

    print(len(result))

    with open('result.json', 'w') as f:
        json.dump(result, f)

if __name__ == "__main__":
    import resource
    rlimit = resource.getrlimit(resource.RLIMIT_NOFILE)
    resource.setrlimit(resource.RLIMIT_NOFILE, (1000000, rlimit[1]))
    opt_e = get_args_efficientdet()
    opt_a = get_args_arcface()
    test(opt_a, opt_e)
    # import cv2
    # import os
    # import torchvision.transforms as transforms
    # opt_a.network = 'resnet_cbam'
    # backbone = ResNetCBAM(opt_a)
    # b_name = opt_a.network+'_{}'.format(opt_a.num_layers_c)
    # backbone.load_state_dict(torch.load(os.path.join(opt_a.saved_path, b_name+'.pth')))
    # backbone.eval()
    # with open('result.json', 'r') as f:
    #     d = json.load(f)
    # print(d['001652'])
    # transform = transforms.Normalize(
    #         mean=[0.55574415, 0.51230767, 0.51123354], 
    #         std=[0.21303795, 0.21604613, 0.21273348])
    # img = cv2.imread('data/validation_images/021459_5.jpg')
    # img = img[131:556,220:438, :]
    # img = cv2.resize(img, (224,224))
    # cv2.imwrite('bbb.jpg', img)
    # det = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
    # det = det.astype(np.float32) / 255

    # det = torch.from_numpy(det)
    # det = det.permute(2, 0, 1)

    # det = transform(det).unsqueeze(0)
    
    # # img = cv2.rectangle(img, (400, 141), (523, 488), (255,0,0), 3)
    # output_1 = backbone(det)

    # img = cv2.imread('data/validation_videos/001652_160.jpg')
    # img = img[332:719,184:342, :]
    # img = cv2.resize(img, (224,224))
    # cv2.imwrite('aaa.jpg', img)
    # det = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
    # det = det.astype(np.float32) / 255

    # det = torch.from_numpy(det)
    # det = det.permute(2, 0, 1)

    # det = transform(det).unsqueeze(0)
    
    # # img = cv2.rectangle(img, (400, 141), (523, 488), (255,0,0), 3)
    # output_2 = backbone(det)
    # print(output_2@output_1.t())
    
