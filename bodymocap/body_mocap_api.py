# Copyright (c) Facebook, Inc. and its affiliates.

import cv2
import sys
import torch
import numpy as np
import pickle
from torchvision.transforms import Normalize

from bodymocap.models import hmr, SMPLX
from bodymocap import constants
from bodymocap.utils.imutils import crop, crop_bboxInfo, process_image_bbox, process_image_keypoints, bbox_from_keypoints
from mocap_utils.coordconv import convert_smpl_to_bbox, convert_bbox_to_oriIm
import mocap_utils.geometry_utils as gu
from alfred.dl.torch.common import device, print_shape
import os
from .utils.utils import ORTWrapper
from nosmpl.smpl import SMPL

from .constants import JOINT_MAP, JOINT_NAMES

torch.set_grad_enabled(False)


class BodyMocap(object):
    def __init__(self, regressor_checkpoint, smpl_dir, device=torch.device('cuda'), use_smplx=False):
        self.device = device

        # Load parametric model (SMPLX or SMPL)
        if use_smplx:
            smplModelPath = smpl_dir + '/SMPLX_NEUTRAL.pkl'
            self.smpl = SMPLX(smpl_dir,
                              batch_size=1,
                              num_betas=10,
                              use_pca=False,
                              create_transl=False).to(self.device)
            self.use_smplx = True
        else:
            smplModelPath = smpl_dir + '/basicModel_neutral_lbs_10_207_0_v1.0.0.pkl'
            # self.smpl = SMPL(smplModelPath, batch_size=1,
            #                  create_transl=False).to(self.device)
            smpl_onnx_file = smplModelPath.replace('pkl', 'onnx')
            self.joints_map = [constants.JOINT_MAP[i] for i in constants.JOINT_NAMES]
            self.use_smplx = False
            if os.path.exists(smpl_onnx_file):
                self.onnx_smpl = True
                self.smpl = ORTWrapper(smpl_onnx_file)
            else:
                self.onnx_smpl = False
                self.smpl = SMPL(smplModelPath, extra_regressor='extra_data/body_module/data_from_spin/J_regressor_extra.npy').to(self.device)
                self.joints_map = torch.as_tensor(self.joints_map).to(device)

                a = torch.rand([1, 10]).to(device)
                b = torch.rand([1, 24, 3, 3]).to(device)
                torch.onnx.export(self.smpl, (a, b),
                                smpl_onnx_file,
                                output_names=['verts', 'joints', 'faces'],
                                opset_version=12)
                print('an onnx saved into: ', smpl_onnx_file)

        # Load pre-trained neural network
        SMPL_MEAN_PARAMS = './extra_data/body_module/data_from_spin/smpl_mean_params.npz'
        self.model_regressor_onnx_file = regressor_checkpoint.replace(
            'pt', 'onnx')
        if os.path.exists(self.model_regressor_onnx_file):
            self.onnx = True
            self.model_regressor = ORTWrapper(self.model_regressor_onnx_file)
        else:
            self.onnx = False
            self.model_regressor = hmr(SMPL_MEAN_PARAMS).to(self.device)
            checkpoint = torch.load(regressor_checkpoint, map_location='cpu')
            self.model_regressor.load_state_dict(
                checkpoint['model'], strict=False)
            self.model_regressor.eval()

            # export to onnx
            a = torch.rand([1, 3, 224, 224]).to(device)
            torch.onnx.export(self.model_regressor, a,
                              self.model_regressor_onnx_file,
                              output_names=['rotmat', 'shape', 'cam'],
                              opset_version=12)
            print('an onnx saved into: ', self.model_regressor_onnx_file)

    def regress(self, img_original, body_bbox_list):
        """
            args: 
                img_original: original raw image (BGR order by using cv2.imread)
                body_bbox: bounding box around the target: (minX, minY, width, height)
            outputs:
                pred_vertices_img:
                pred_joints_vis_img:
                pred_rotmat
                pred_betas
                pred_camera
                bbox: [bbr[0], bbr[1],bbr[0]+bbr[2], bbr[1]+bbr[3]])
                bboxTopLeft:  bbox top left (redundant)
                boxScale_o2n: bbox scaling factor (redundant) 
        """
        pred_output_list = list()

        for body_bbox in body_bbox_list:
            img, norm_img, boxScale_o2n, bboxTopLeft, bbox = process_image_bbox(
                img_original, body_bbox, input_res=constants.IMG_RES)
            bboxTopLeft = np.array(bboxTopLeft)

            # bboxTopLeft = bbox['bboxXYWH'][:2]
            if img is None:
                pred_output_list.append(None)
                continue

            # model forward
            if self.onnx:
                preds = self.model_regressor.infer(norm_img.cpu().numpy())
                pred_rotmat = preds['rotmat']
                pred_betas = preds['shape']
                pred_camera = preds['cam']
            else:
                pred_rotmat, pred_betas, pred_camera = self.model_regressor(
                    norm_img.to(self.device))

                pred_rotmat = torch.as_tensor(pred_rotmat).to(device)
                pred_betas = torch.as_tensor(pred_betas).to(device)
                pred_camera = torch.as_tensor(pred_camera).to(device)

            # Convert rot_mat to aa since hands are always in aa
            # pred_aa = rotmat3x3_to_angle_axis(pred_rotmat)
            # pred_aa = gu.rotation_matrix_to_angle_axis(pred_rotmat).cuda()
            # pred_aa = gu.rotation_matrix_to_angle_axis(pred_rotmat).to(device)
            # pred_aa = pred_aa.reshape(pred_aa.shape[0], 72)
            # smpl_output = self.smpl(
            #     betas=pred_betas,
            #     body_pose=pred_aa[:, 3:],
            #     global_orient=pred_aa[:, :3],
            #     pose2rot=True)
            # pred_vertices = smpl_output.vertices
            # pred_joints_3d = smpl_output.joints
            
            if self.onnx_smpl:
                preds = self.smpl.infer(
                    [pred_betas, pred_rotmat]
                )
                pred_vertices = preds['verts'] 
                pred_joints_3d = preds['joints']
                faces = preds['faces']
                pred_joints_3d = pred_joints_3d[:, self.joints_map, :]
                pred_vertices = pred_vertices[0]
                pred_joints_3d = pred_joints_3d[0]
                print_shape(pred_vertices, pred_joints_3d)
                pred_camera = pred_camera.ravel()
            else:
                print_shape(pred_betas, pred_rotmat)
                pred_vertices, pred_joints_3d, faces = self.smpl(
                    pred_betas, pred_rotmat
                )
                pred_joints_3d = pred_joints_3d[:, self.joints_map, :]

                pred_vertices = pred_vertices[0].cpu().numpy()
                pred_camera = pred_camera.cpu().numpy().ravel()
                pred_joints_3d = pred_joints_3d[0].cpu().numpy()  # (1,49,3)

                pred_rotmat = pred_rotmat.cpu().numpy()
                faces = faces.cpu().numpy()

            camScale = pred_camera[0]  # *1.15
            camTrans = pred_camera[1:]

            pred_output = dict()
            # Convert mesh to original image space (X,Y are aligned to image)
            # 1. SMPL -> 2D bbox
            # 2. 2D bbox -> original 2D image
            pred_vertices_bbox = convert_smpl_to_bbox(
                pred_vertices, camScale, camTrans)
            pred_vertices_img = convert_bbox_to_oriIm(
                pred_vertices_bbox, boxScale_o2n, bboxTopLeft, img_original.shape[1], img_original.shape[0])

            # Convert joint to original image space (X,Y are aligned to image)
            pred_joints_vis = pred_joints_3d[:, :3]  # (49,3)
            pred_joints_vis_bbox = convert_smpl_to_bbox(
                pred_joints_vis, camScale, camTrans)
            pred_joints_vis_img = convert_bbox_to_oriIm(
                pred_joints_vis_bbox, boxScale_o2n, bboxTopLeft, img_original.shape[1], img_original.shape[0])

            # Output
            pred_output['img_cropped'] = img[:, :, ::-1]
            # SMPL vertex in original smpl space
            pred_output['pred_vertices_smpl'] = pred_vertices
            # SMPL vertex in image space
            pred_output['pred_vertices_img'] = pred_vertices_img
            # SMPL joints in image space
            pred_output['pred_joints_img'] = pred_joints_vis_img

            pred_aa_tensor = gu.rotation_matrix_to_angle_axis(
                pred_rotmat[0])
            pred_output['pred_body_pose'] = pred_aa_tensor.reshape(1, 72)  # (1, 72)

            pred_output['pred_rotmat'] = pred_rotmat # (1, 24, 3, 3)
            pred_output['pred_betas'] = pred_betas  # (1, 10)

            pred_output['pred_camera'] = pred_camera
            pred_output['bbox_top_left'] = bboxTopLeft
            pred_output['bbox_scale_ratio'] = boxScale_o2n
            pred_output['faces'] = faces

            if self.use_smplx:
                img_center = np.array(
                    (img_original.shape[1], img_original.shape[0])) * 0.5
                # right hand
                pred_joints = smpl_output.right_hand_joints[0].cpu().numpy()
                pred_joints_bbox = convert_smpl_to_bbox(
                    pred_joints, camScale, camTrans)
                pred_joints_img = convert_bbox_to_oriIm(
                    pred_joints_bbox, boxScale_o2n, bboxTopLeft, img_original.shape[1], img_original.shape[0])
                pred_output['right_hand_joints_img_coord'] = pred_joints_img
                # left hand
                pred_joints = smpl_output.left_hand_joints[0].cpu().numpy()
                pred_joints_bbox = convert_smpl_to_bbox(
                    pred_joints, camScale, camTrans)
                pred_joints_img = convert_bbox_to_oriIm(
                    pred_joints_bbox, boxScale_o2n, bboxTopLeft, img_original.shape[1], img_original.shape[0])
                pred_output['left_hand_joints_img_coord'] = pred_joints_img

            pred_output_list.append(pred_output)

        return pred_output_list

    def get_hand_bboxes(self, pred_body_list, img_shape):
        """
            args: 
                pred_body_list: output of body regresion
                img_shape: img_height, img_width
            outputs:
                hand_bbox_list: 
        """
        hand_bbox_list = list()
        for pred_body in pred_body_list:
            hand_bbox = dict(
                left_hand=None,
                right_hand=None
            )
            if pred_body is None:
                hand_bbox_list.append(hand_bbox)
            else:
                for hand_type in hand_bbox:
                    key = f'{hand_type}_joints_img_coord'
                    pred_joints_vis_img = pred_body[key]

                    if pred_joints_vis_img is not None:
                        # get initial bbox
                        x0, x1 = np.min(pred_joints_vis_img[:, 0]), np.max(
                            pred_joints_vis_img[:, 0])
                        y0, y1 = np.min(pred_joints_vis_img[:, 1]), np.max(
                            pred_joints_vis_img[:, 1])
                        width, height = x1-x0, y1-y0
                        # extend the obtained bbox
                        margin = int(max(height, width) * 0.2)
                        img_height, img_width = img_shape
                        x0 = max(x0 - margin, 0)
                        y0 = max(y0 - margin, 0)
                        x1 = min(x1 + margin, img_width)
                        y1 = min(y1 + margin, img_height)
                        # result bbox in (x0, y0, w, h) format
                        hand_bbox[hand_type] = np.array(
                            [x0, y0, x1-x0, y1-y0])  # in (x, y, w, h ) format

                hand_bbox_list.append(hand_bbox)

        return hand_bbox_list
