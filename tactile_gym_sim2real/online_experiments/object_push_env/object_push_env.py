import os, inspect
import sys
from pathlib import Path
import math
import gym
import numpy as np
import time
import random
import pkgutil
from pkg_resources import parse_version
import cv2
import imageio
import matplotlib.pyplot as plt
import cri
import pybullet as pb

from tactile_gym_sim2real.online_experiments.ur5_tactip import UR5_TacTip
from tactile_gym_sim2real.online_experiments.gan_net import pix2pix_GAN

from tactile_gym.assets import get_assets_path, add_assets_path

from robopush.camera import RSCamera, ColorFrameError, DepthFrameError, DistanceError
from robopush.detector import ArUcoDetector
from robopush.tracker import ArUcoTracker, display_fn, NoMarkersDetected, MultipleMarkersDetected
from robopush.utils import Namespace, transform_euler, inv_transform_euler

RS_RESOLUTION = (640, 480)
FPS = 10.0
def make_realsense():
    return RSCamera(color_size=RS_RESOLUTION, color_fps=60, depth_size=RS_RESOLUTION, depth_fps=60)

class ObjectPushEnv(gym.Env):

    def __init__(self,
                 env_modes,
                 gan_model_dir,
                 GanGenerator,
                 max_steps=1000,
                 rl_image_size=[64,64],
                 show_plot=True):

        self._observation = []
        self._env_step_counter = 0
        self._max_steps = max_steps
        self.rl_image_size = rl_image_size
        self.show_plot = show_plot
        self.first_run = True
        self.tactip_type = 'right_angle'

        # for incrementing workframe each episode
        self.reset_counter = -1

        # flags for saving data
        self.record_video_flag = False
        self.save_traj_flag = True
        self.save_rs_data_flag = True

        if self.record_video_flag:
            self.video_frames = []

        # define the movement mode used in the saved model
        self.movement_mode = env_modes['movement_mode']
        self.control_mode  = env_modes['control_mode']

        # what traj to generate
        # self.traj_type = 'straight'
        # self.traj_type = 'curve'
        self.traj_type = 'sin'

        # set the workframe for the tool center point origin
        # self.work_frame = [0.0, -420.0, 200, -180, 0, 0] # safe
        self.work_frame = [-200.0, -420.0, 55, -180, 0, 0] # x on blue mat

        # add rotation to yaw in order to allign camera without changing workframe
        self.sensor_offset_ang = 45

        # set limits for the tool center point (rel to workframe)
        self.TCP_lims = np.zeros(shape=(6,2))
        self.TCP_lims[0,0], self.TCP_lims[0,1] = -0.0, 300.0  # x lims
        self.TCP_lims[1,0], self.TCP_lims[1,1] = -100.0, 100.0  # y lims
        self.TCP_lims[2,0], self.TCP_lims[2,1] = 0.0, 0.0     # z lims
        self.TCP_lims[3,0], self.TCP_lims[3,1] = 0.0, 0.0     # roll lims
        self.TCP_lims[4,0], self.TCP_lims[4,1] = 0.0, 0.0     # pitch lims
        self.TCP_lims[5,0], self.TCP_lims[5,1] = self.sensor_offset_ang - 45, self.sensor_offset_ang + 45    # yaw lims

        # setup action space to match sim
        self.setup_action_space()

        # load the trained pix2pix GAN network
        self.GAN = pix2pix_GAN(
            gan_model_dir=gan_model_dir,
            Generator=GanGenerator,
            rl_image_size=self.rl_image_size
        )

        # load saved border image files
        ref_images_path = add_assets_path(
            os.path.join('robot_assets', 'tactip', 'tactip_reference_images', 'right_angle')
        )

        border_gray_savefile = os.path.join( ref_images_path, str(self.rl_image_size[0]) + 'x' + str(self.rl_image_size[0]), 'nodef_gray.npy')
        border_mask_savefile = os.path.join( ref_images_path, str(self.rl_image_size[0]) + 'x' + str(self.rl_image_size[0]), 'border_mask.npy')
        self.border_gray = np.load(border_gray_savefile)
        self.border_mask = np.load(border_mask_savefile)

        # setup plot for rendering
        if self.show_plot:
            cv2.namedWindow('real_vs_generated')
            self._render_closed = False
        else:
            self._render_closed = True

        # setup the UR5
        self._UR5 = UR5_TacTip(control_mode=self.control_mode,
                               workframe=self.work_frame,
                               TCP_lims=self.TCP_lims,
                               sensor_offset_ang=self.sensor_offset_ang,
                               action_lims=[self.min_action, self.max_action],
                               tactip_type=self.tactip_type)


        # this is needed to set some variables used for initial observation/obs_dim()
        self.reset()

        # set the observation space
        self.setup_observation_space()

        self.seed()

        # initialise realsens camera
        if self.save_rs_data_flag:
            self.setup_realsense()

    def setup_realsense(self):


        # setup the realsense camera for capturing qunatitative data
        self.rs_camera = make_realsense()
        self.rs_detector = ArUcoDetector(self.rs_camera, marker_length=25.0, dict_id=cv2.aruco.DICT_7X7_50)
        self.rs_tracker = ArUcoTracker(self.rs_detector, track_attempts=30, display_fn=None)

        # load extrinsic camera params
        root_dir = Path(os.path.join('realsense_params'))
        # extrinsics_dir = root_dir/"dynamics/calib/calib_05251104"
        extrinsics_dir = root_dir/"dynamics/calib/calib_06041016"

        ext = Namespace()
        ext.load(extrinsics_dir/"extrinsics.pkl")

        # convert extrinsic camera params to 4x4 homogeneous matrices
        self.rs = Namespace()
        self.rs.ext_rvec = ext.rvec
        self.rs.ext_tvec = ext.tvec
        self.rs.ext_rmat, _ = cv2.Rodrigues(np.array(self.rs.ext_rvec, dtype=np.float64))
        self.rs.t_cam_base = np.hstack((self.rs.ext_rmat, np.array(self.rs.ext_tvec, dtype=np.float64).reshape((-1, 1))))
        self.rs.t_cam_base = np.vstack((self.rs.t_cam_base, np.array((0.0, 0.0, 0.0, 1.0)).reshape(1, -1)))
        self.rs.t_base_cam = np.linalg.pinv(self.rs.t_cam_base)

        # create a save dir
        self.rs_save_dir = os.path.join(
            'collected_data',
            'rs_data'
        )
        os.makedirs(self.rs_save_dir, exist_ok=True)
        rs_video_file = os.path.join(self.rs_save_dir, 'rs_video.mp4')

        # Initialise tracking data
        [
            self.rs.work_align,
            self.rs.corners,
            self.rs.ids,
            self.rs.cam_poses,
            self.rs.base_poses,
            self.rs.centroids,
            self.rs.cam_centroids,
            self.rs.base_centroids
        ] = [], [], [], [], [], [], [], []

        # setup video writer
        self.rs_vid_out = cv2.VideoWriter(
            rs_video_file,
            cv2.VideoWriter_fourcc(*'mp4v'),
            FPS,
            RS_RESOLUTION
        )


    def get_realsense_data(self):

        try:
            self.rs_tracker.track()
        except (ColorFrameError, DepthFrameError, DistanceError, \
                NoMarkersDetected, MultipleMarkersDetected) as e:
                print(e)
                sys.exit('Issue with Realsense Tracking.')

        # grab data needed for object tracking
        if self.save_rs_data_flag:

            # compute marker centroid position and pose in base frame
            cam_centroid = self.rs_tracker.centroid_position

            base_centroid = None
            if cam_centroid is not None:
                camera_point_h = np.vstack((np.array(cam_centroid).reshape((-1, 1)), (1,)))
                base_centroid = np.dot(self.rs.t_base_cam, camera_point_h).squeeze()[:3]

            cam_pose = self.rs_tracker.pose
            base_pose = None
            if cam_pose is not None:
                base_pose = np.dot(self.rs.t_base_cam, cam_pose)

            # Capture ArUco tracking data
            self.rs.corners.append(self.rs_tracker.corners)
            self.rs.ids.append(self.rs_tracker.ids)
            self.rs.centroids.append(self.rs_tracker.centroid)
            self.rs.cam_centroids.append(cam_centroid)
            self.rs.base_centroids.append(base_centroid)
            self.rs.cam_poses.append(cam_pose)
            self.rs.base_poses.append(base_pose)

            # write video frame
            rs_rgb_frame = self.rs_camera.color_image
            self.rs_vid_out.write(rs_rgb_frame)

    def save_rs_data(self):
        if self.save_rs_data_flag:

            # Convert tracking data to numpy arrays
            self.rs.corners = np.array(self.rs.corners)
            self.rs.ids = np.array(self.rs.ids)
            self.rs.centroids = np.array(self.rs.centroids)
            self.rs.cam_centroids = np.array(self.rs.cam_centroids)
            self.rs.base_centroids = np.array(self.rs.base_centroids)
            self.rs.cam_poses = np.array(self.rs.cam_poses)
            self.rs.base_poses = np.array(self.rs.base_poses)

            # save tracking data
            self.rs.save(
                os.path.join(self.rs_save_dir, "rs_data.pkl")
            )

            # release the video writer
            self.rs_vid_out.release()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()

    def close(self):

        # save recorded video
        if self.record_video_flag and self.video_frames != []:
            video_file = os.path.join('collected_data', 'tactile_video.mp4')
            imageio.mimwrite(video_file, np.stack(self.video_frames), fps=FPS)

        # Realsense data
        self.save_rs_data()

        self._UR5.close()

    def seed(self, seed=None):
        self.np_random, seed = gym.utils.seeding.np_random(seed)
        return [seed]

    def setup_action_space(self):

        # these are used for bounds on the action space in SAC and clipping
        # range for PPO
        self.min_action, self.max_action  = -0.25,  0.25

        # define action ranges per act dim to rescale output of policy
        if self.control_mode == 'TCP_position_control':

            max_pos_change = 1
            max_ang_change = 1 * (np.pi/180) # degree per step

            self.x_act_min, self.x_act_max = -max_pos_change, max_pos_change
            self.y_act_min, self.y_act_max = -max_pos_change, max_pos_change
            self.z_act_min, self.z_act_max = 0, 0
            self.roll_act_min,  self.roll_act_max  = 0, 0
            self.pitch_act_min, self.pitch_act_max = 0, 0
            self.yaw_act_min,   self.yaw_act_max   = -max_ang_change, max_ang_change

        elif self.control_mode == 'TCP_velocity_control':

            # approx sim_vel / 1.6
            max_pos_vel = 5                # mm/s
            max_ang_vel = 2.5  * (np.pi/180) # rad/s

            self.x_act_min, self.x_act_max = -max_pos_vel, max_pos_vel
            self.y_act_min, self.y_act_max = -max_pos_vel, max_pos_vel
            self.z_act_min, self.z_act_max = 0, 0
            self.roll_act_min,  self.roll_act_max  = 0, 0
            self.pitch_act_min, self.pitch_act_max = 0, 0
            self.yaw_act_min,   self.yaw_act_max   = -max_ang_vel, max_ang_vel

        # setup action space
        self.act_dim = self.get_act_dim()
        self.action_space = gym.spaces.Box(low=self.min_action,
                                           high=self.max_action,
                                           shape=(self.act_dim,),
                                           dtype=np.float32)

    def setup_observation_space(self):

        # image dimensions for sensor
        self.tactile_obs_dim = self.get_tactile_obs().shape
        self.feature_obs_dim = self.get_feature_obs().shape

        self.observation_space = gym.spaces.Dict({
            'tactile': gym.spaces.Box(
                low=0, high=255, shape=self.tactile_obs_dim, dtype=np.uint8
            ),
            'extended_feature': gym.spaces.Box(
                low=-np.inf, high=np.inf, shape=self.feature_obs_dim, dtype=np.float32
            )
        })

    def setup_traj(self):
        self.traj_n_points = 10
        self.traj_spacing = 0.025

        self.goal_update_rate = 50

        # setup traj arrays
        self.targ_traj_list_id = 0
        self.traj_pos_workframe = np.zeros(shape=(self.traj_n_points,3))
        self.traj_rpy_workframe = np.zeros(shape=(self.traj_n_points,3))

        if self.traj_type == 'straight':
            self.load_trajectory_straight()
        elif self.traj_type == 'curve':
            self.load_trajectory_curve()
        elif self.traj_type == 'sin':
            self.load_trajectory_sin()
        else:
            sys.exit('Incorrect traj_type specified: {}'.format(self.traj_type))

        traj_idx = int(self.reset_counter / 2)
        if self.save_traj_flag:
            np.save('collected_data/traj_pos_{}.npy'.format(traj_idx), self.traj_pos_workframe)
            np.save('collected_data/traj_rpy_{}.npy'.format(traj_idx), self.traj_rpy_workframe)

        self.update_goal()

    def load_trajectory_straight(self):

        # randomly pick traj direction
        # traj_ang = np.random.uniform(-np.pi/8, np.pi/8)
        traj_angs = [-np.pi/8, 0.0, np.pi/8, 0.0]
        traj_idx = int(self.reset_counter / 2)
        init_offset = 0.04 + self.traj_spacing

        for i in range(int(self.traj_n_points)):

            traj_ang = traj_angs[traj_idx]

            dir_x = np.cos(traj_ang)
            dir_y  = np.sin(traj_ang)
            dist = (i*self.traj_spacing)

            x = init_offset + dist*dir_x
            y = dist*dir_y
            z = 0.0
            self.traj_pos_workframe[i] = [x, y, z]

        # calc orientation to place object at
        self.traj_rpy_workframe[:,2] = np.gradient(self.traj_pos_workframe[:,1], self.traj_spacing)

    def load_trajectory_curve(self):

        # pick traj direction
        traj_idx = int(self.reset_counter / 2)
        curve_dir = -1 if traj_idx % 2 == 0 else +1

        def curve_func(x):
            y = curve_dir*x**2
            return y

        init_offset = 0.04 + self.traj_spacing

        for i in range(int(self.traj_n_points)):
            dist = (i*self.traj_spacing)
            x = init_offset + dist
            y = curve_func(x)
            z = 0.0
            self.traj_pos_workframe[i] = [x, y, z]

        # calc orientation to place object at
        self.traj_rpy_workframe[:,2] = np.gradient(self.traj_pos_workframe[:,1], self.traj_spacing)

    def load_trajectory_sin(self):

        #  pick traj direction
        traj_idx = int(self.reset_counter / 2)
        curve_dir = -1 if traj_idx % 2 == 0 else +1
        init_offset = 0.04 + self.traj_spacing

        def curve_func(x):
            y = curve_dir*0.025*np.sin(20*(x-init_offset))
            return y


        for i in range(int(self.traj_n_points)):
            dist = (i*self.traj_spacing)
            x = init_offset + dist
            y = curve_func(x)
            z = 0.0
            self.traj_pos_workframe[i] = [x, y, z]

        # calc orientation to place object at
        self.traj_rpy_workframe[:,2] = np.gradient(self.traj_pos_workframe[:,1], self.traj_spacing)


    def update_goal(self):

        # increment targ list
        self.targ_traj_list_id += 1

        if self.targ_traj_list_id >= self.traj_n_points-1:
            self.targ_traj_list_id = self.traj_n_points-1

        # create variables for goal pose in workframe to use later
        self.goal_pos_workframe = self.traj_pos_workframe[self.targ_traj_list_id]
        self.goal_rpy_workframe = self.traj_rpy_workframe[self.targ_traj_list_id]


    def reset(self):

        self._env_step_counter = 0

        # increment reset counter for iterating through directions
        self.reset_counter += 1

        # reset the ur5 arm
        self._UR5.reset()

        # reset the goal
        self.setup_traj()

        # get the starting observation
        self._observation = self.get_observation()

        # use to avoid doing things on first call to reset
        self.first_run = False

        return self._observation

    def get_tip_direction_workframe(self):
        """
        Warning, deadline research code (specific to current workframe)
        """
        # get rotation from current tip orientation
        current_tip_pose = self._UR5.current_TCP_pose

        # angle for perp and par vectors
        par_ang  = ( current_tip_pose[5] + self.sensor_offset_ang ) * np.pi/180
        perp_ang = ( current_tip_pose[5] + self.sensor_offset_ang - 90 ) * np.pi/180

        # create vectors (directly in workframe) pointing in perp and par directions of current sensor
        workframe_par_tip_direction  = np.array([np.cos(par_ang),  np.sin(par_ang), 0]) # vec pointing outwards from tip
        workframe_perp_tip_direction = np.array([np.cos(perp_ang), np.sin(perp_ang),0]) # vec pointing perp to tip

        return workframe_par_tip_direction, workframe_perp_tip_direction

    def encode_TCP_frame_actions(self, actions):
        """
        Warning, deadline research code (specific to current workframe)
        """

        encoded_actions = np.zeros(6)

        workframe_par_tip_direction, workframe_perp_tip_direction = self.get_tip_direction_workframe()

        if self.movement_mode == 'TyRz':

            # translate the direction
            perp_scale = actions[0]
            perp_action = np.dot(workframe_perp_tip_direction, perp_scale)

            # auto move in the dir tip is pointing
            # par_scale = 1.0 # always at max
            par_scale = 1.0*self.max_action
            par_action = np.dot(workframe_par_tip_direction, par_scale)

            encoded_actions[0] += perp_action[0] + par_action[0]
            encoded_actions[1] += perp_action[1] + par_action[1]
            encoded_actions[5] += actions[1]

        elif self.movement_mode == 'TxTyRz':

            # translate the direction
            perp_scale = actions[1]
            perp_action = np.dot(workframe_perp_tip_direction, perp_scale)

            par_scale = actions[0]
            par_action = np.dot(workframe_par_tip_direction, par_scale)

            encoded_actions[0] += perp_action[0] + par_action[0]
            encoded_actions[1] += perp_action[1] + par_action[1]
            encoded_actions[5] += actions[2]

        return encoded_actions

    def encode_work_frame_actions(self, actions):
        """
        Return actions as np.array in correct places for sending to ur5.
        """

        encoded_actions = np.zeros(6)

        if self.movement_mode == 'y':
            encoded_actions[0] = self.max_action
            encoded_actions[1] = actions[0]

        if self.movement_mode == 'yRz':
            encoded_actions[0] = self.max_action
            encoded_actions[1] = actions[0]
            encoded_actions[5] = actions[1]

        elif self.movement_mode == 'xyRz':
            encoded_actions[0] = actions[0]
            encoded_actions[1] = actions[1]
            encoded_actions[5] = actions[2]

        return encoded_actions

    def scale_actions(self, actions):

        # would prefer to enforce action bounds on algorithm side, but this is ok for now
        actions = np.clip(actions, self.min_action, self.max_action)

        input_range = (self.max_action - self.min_action)

        new_x_range = (self.x_act_max - self.x_act_min)
        new_y_range = (self.y_act_max - self.y_act_min)
        new_z_range = (self.z_act_max - self.z_act_min)
        new_roll_range  = (self.roll_act_max  - self.roll_act_min)
        new_pitch_range = (self.pitch_act_max - self.pitch_act_min)
        new_yaw_range   = (self.yaw_act_max   - self.yaw_act_min)

        scaled_actions = [
            (((actions[0] - self.min_action) * new_x_range) / input_range) + self.x_act_min,
            (((actions[1] - self.min_action) * new_y_range) / input_range) + self.y_act_min,
            (((actions[2] - self.min_action) * new_z_range) / input_range) + self.z_act_min,
            (((actions[3] - self.min_action) * new_roll_range)  / input_range) + self.roll_act_min,
            (((actions[4] - self.min_action) * new_pitch_range) / input_range) + self.pitch_act_min,
            (((actions[5] - self.min_action) * new_yaw_range)   / input_range) + self.yaw_act_min,
        ] # 6 dim when sending to ur5

        return np.array(scaled_actions)

    def step(self, action):

        # scale and embed actions appropriately
        if self.movement_mode in ['y', 'yRz', 'xyRz']:
            encoded_actions = self.encode_work_frame_actions(action)
        elif self.movement_mode in ['TyRz', 'TxTyRz']:
            encoded_actions = self.encode_TCP_frame_actions(action)

        scaled_actions  = self.scale_actions(encoded_actions)

        self._env_step_counter += 1

        # send action to ur5
        if self.control_mode == 'TCP_position_control':
            self._UR5.apply_position_action(scaled_actions)

        elif self.control_mode == 'TCP_velocity_control':
            self._UR5.apply_velocity_action(scaled_actions)

        # pull info after step
        done = self.termination()
        reward = self.reward()
        self._observation = self.get_observation()

        if self._env_step_counter % self.goal_update_rate == 0:
            self.update_goal()

        # update data using realsense
        if self.save_rs_data_flag:
            self.get_realsense_data()

        return self._observation, reward, done, {}

    def termination(self):
        # terminate when max ep len reached
        if self._env_step_counter >= self._max_steps:
            return True
        return False

    def reward(self):
        return 0

    def get_tactile_obs(self):
        # get image from sensor
        observation = self._UR5.get_observation()

        # process with gan here
        generated_sim_image, processed_real_image = self.GAN.gen_sim_image(observation)

        # add border to image
        generated_sim_image[self.border_mask==1] = self.border_gray[self.border_mask==1]

        # add a channel axis at end
        generated_sim_image = generated_sim_image[..., np.newaxis]

        # plot data
        if not self._render_closed:
            # resize to 256, 256 for video
            resized_real_image = cv2.resize(processed_real_image,
                                           (256,256),
                                           interpolation=cv2.INTER_NEAREST)
            resized_sim_image = cv2.resize(generated_sim_image,
                                           (256,256),
                                           interpolation=cv2.INTER_NEAREST)
            frame = np.hstack([resized_real_image, resized_sim_image])
            cv2.imshow('real_vs_generated', frame)
            if cv2.waitKey(1) & 0xFF == 27:
                cv2.destroyWindow('real_vs_generated')
                self._render_closed = True

            if self.record_video_flag:
                self.video_frames.append(frame)

        return generated_sim_image

    def get_feature_obs(self):
        """
        Get feature to extend current observations.
        """
        # get pose in workframe
        robot_pose = self._UR5.current_TCP_pose
        robot_pose[5] -= self.sensor_offset_ang

        # Hacky and shouldnt really work but it does
        tcp_pos_workframe = robot_pose[:3] * 0.001
        tcp_rpy_workframe = robot_pose[3:] * np.pi/180

        feature_array = np.array([*tcp_pos_workframe,  *tcp_rpy_workframe,
                                  *self.goal_pos_workframe, *self.goal_rpy_workframe
                                  ])

        return feature_array

    def get_observation(self):
        """
        Returns the observation
        """
        # init obs dict
        observation = {}
        observation['tactile'] = self.get_tactile_obs()
        observation['extended_feature'] = self.get_feature_obs()
        return observation

    def get_act_dim(self):
        if self.movement_mode == 'y':
            return 1
        elif self.movement_mode == 'yRz':
            return 2
        elif self.movement_mode == 'xyRz':
            return 3
        if self.movement_mode == 'TyRz':
            return 2
        if self.movement_mode == 'TxTyRz':
            return 3
        else:
            sys.exit('Incorrect movement mode specified: {}'.format(self.movement_mode))
