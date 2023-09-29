import time
from collections import OrderedDict, defaultdict
from typing import Dict, List, Sequence

import gym
import numpy as np
import pyrealsense2 as rs
import open3d as o3d

from mani_skill2.envs.sapien_env import BaseEnv as MS2BaseEnv
from real_robot.utils.common import (
    convert_observation_to_space, vectorize_pose, flatten_state_dict
)
from real_robot.utils.visualization import Visualizer
from real_robot.sensors.camera import (
    CALIB_CAMERA_POSES,
    CameraConfig,
    Camera,
    parse_camera_cfgs
)
from real_robot.agents import XArm7
from real_robot.utils.multiprocessing import ctx


class XArmBaseEnv(gym.Env):
    """Superclass for XArm real robot environments."""

    SUPPORTED_OBS_MODES = ("state", "state_dict", "none", "image")
    SUPPORTED_IMAGE_OBS_MODES = ("hand_front", "front", "hand")

    def __init__(
        self, *args,
        obs_mode=None,
        image_obs_mode=None,
        control_mode="pd_ee_delta_pos",
        motion_mode="position",
        xarm_ip="192.168.1.229",
        safety_boundary_mm: List[int] = [550, 0, 50, -600, 280, 0],
        boundary_clip_mm: int = 10,
        with_hand_camera: bool = True,
        action_translation_scale=100.0,
        action_axangle_scale=0.1,
        **kwargs
    ):
        """
        :param control_mode: xArm control mode (determines set_action type)
        :param motion_mode: xArm motion mode (determines xArm motion mode)
        :param xarm_ip: xArm7 ip address, see controller box
        :param safety_boundary_mm: [x_max, x_min, y_max, y_min, z_max, z_min] (mm)
        :param boundary_clip_mm: clip action when TCP position to boundary is
                                 within boundary_clip_eps (mm). No clipping if None.
        :param with_hand_camera: whether to include hand camera mount in TCP offset.
        :param translation_scale: action [-1, 1] maps to [-100mm, 100mm],
                                  Used for delta control_mode only.
        :param axangle_scale: axangle action norm (rotation angle) is multiplied by 0.1
                              [-1, 0, 0] => rotate around [1, 0, 0] by -0.1 rad
                              Used for delta control_mode only.
        """
        super().__init__(*args, **kwargs)

        self._is_ms2_env = isinstance(self, MS2BaseEnv)

        # Check if self._engine exists.
        # If exists, needs to wrap initialization call with
        # if not isinstance(self, XArmBaseEnv):
        #     super().__init__()
        assert not hasattr(self, "_engine"), "sapien.Engine exists"

        # Observation mode
        if obs_mode is None:
            obs_mode = self.SUPPORTED_OBS_MODES[0]
        if obs_mode not in self.SUPPORTED_OBS_MODES:
            raise NotImplementedError(f"Unsupported obs mode: {obs_mode}")
        self._obs_mode = obs_mode

        # Control mode
        self._control_mode = control_mode
        self._motion_mode = motion_mode

        # Image obs mode
        if image_obs_mode is None:
            image_obs_mode = self.SUPPORTED_IMAGE_OBS_MODES[0]
        if image_obs_mode not in self.SUPPORTED_IMAGE_OBS_MODES:
            raise NotImplementedError(
                f"Unsupported image obs mode: {image_obs_mode}"
            )
        self._image_obs_mode = image_obs_mode

        # Reward mode
        if not (self._is_ms2_env or hasattr(self, "SUPPORTED_REWARD_MODES")):
            # Set SUPPORTED_REWARD_MODES if not ms2 env and does not exist
            self.SUPPORTED_REWARD_MODES = ("dense", "sparse")
        reward_mode = kwargs.get("reward_mode", self.SUPPORTED_REWARD_MODES[0])
        if reward_mode not in self.SUPPORTED_REWARD_MODES:
            raise NotImplementedError(
                f"Unsupported reward mode: {reward_mode}"
            )
        self._reward_mode = reward_mode

        # Configure agent and cameras
        self.xarm_ip = xarm_ip
        self.safety_boundary_mm = safety_boundary_mm
        self.boundary_clip_mm = boundary_clip_mm
        self.with_hand_camera = with_hand_camera
        self.action_translation_scale = action_translation_scale
        self.action_axangle_scale = action_axangle_scale
        self._configure_agent()
        self._configure_cameras()
        self._configure_render_cameras()

        # TODO: check Visualizer
        self.visualizer = Visualizer()

        # NOTE: `seed` is deprecated in the latest gym.
        # Use a fixed seed to initialize to enhance determinism
        self.seed(2022)
        obs = self.reset()
        self.observation_space = convert_observation_to_space(obs)
        if self._obs_mode == "image":
            image_obs_space = self.observation_space.spaces["image"]
            for uid, camera in self._cameras.items():
                image_obs_space.spaces[uid] = camera.observation_space
        self.action_space = self.agent.action_space

    def seed(self, seed=None):
        # For each episode, seed can be passed through `reset(seed=...)`,
        # or generated by `_main_rng`
        if seed is None:
            # Explicitly generate a seed for reproducibility
            seed = np.random.RandomState().randint(2**32)
        self._main_seed = seed
        self._main_rng = np.random.RandomState(self._main_seed)
        return [self._main_seed]

    # ---------------------------------------------------------------------- #
    # Configure agent and cameras
    # ---------------------------------------------------------------------- #
    @property
    def control_mode(self):
        return self.agent.control_mode

    def _configure_agent(self):
        """Create real robot agent"""
        self.agent_proc = ctx.Process(
            target=XArm7,
            args=(self.xarm_ip, self._control_mode, self._motion_mode),
            kwargs=dict(
                safety_boundary_mm=self.safety_boundary_mm,
                boundary_clip_mm=self.boundary_clip_mm,
                with_hand_camera=self.with_hand_camera,
                run_as_process=True,
            )
        )
        self.agent_proc.start()
        time.sleep(1.0)  # sleep for 1 second to wait for SharedObject creation

        self.agent = XArm7(
            self.xarm_ip,
            control_mode=self._control_mode, motion_mode=self._motion_mode,
            safety_boundary_mm=self.safety_boundary_mm,
            boundary_clip_mm=self.boundary_clip_mm,
            with_hand_camera=self.with_hand_camera,
        )

    def _register_cameras(self) -> Sequence[CameraConfig]:
        """Register (non-agent) cameras for environment observation."""
        camera_configs = [
            CameraConfig(
                "front_camera", "146322072630",
                CALIB_CAMERA_POSES["front_camera"],
                848, 480, preset="High Accuracy",
                depth_option_kwargs={rs.option.exposure: 1500},
            ),
        ]
        return camera_configs

    def _configure_cameras(self):
        """Configure and create RealSense cameras"""
        self._camera_cfgs = OrderedDict()
        self._camera_cfgs.update(parse_camera_cfgs(self._register_cameras()))

        self._agent_camera_cfgs = parse_camera_cfgs(self.agent.cameras)
        self._camera_cfgs.update(self._agent_camera_cfgs)

        # Select camera_cfgs based on image_obs_mode
        camera_cfgs = OrderedDict()
        if self._image_obs_mode == "front":
            camera_cfgs["front_camera"] = self._camera_cfgs["front_camera"]
        elif self._image_obs_mode == "hand":
            camera_cfgs["hand_camera"] = self._camera_cfgs["hand_camera"]
        elif self._image_obs_mode == "hand_front":
            camera_cfgs["front_camera"] = self._camera_cfgs["front_camera"]
            camera_cfgs["hand_camera"] = self._camera_cfgs["hand_camera"]
        else:
            raise ValueError(f"Unknown image_obs_mode: {self._image_obs_mode}")
        self._camera_cfgs = camera_cfgs

        self._cameras = OrderedDict()
        for uid, camera_cfg in self._camera_cfgs.items():
            self._cameras[uid] = Camera(camera_cfg)

    def _register_render_cameras(self) -> Sequence[CameraConfig]:
        """Register cameras for rendering."""
        return []

    def _configure_render_cameras(self):
        """Configure and create render cameras"""
        self._render_camera_cfgs = parse_camera_cfgs(
            self._register_render_cameras()
        )

        # Cameras for rendering only
        self._render_cameras = OrderedDict()
        for uid, camera_cfg in self._render_camera_cfgs.items():
            self._render_cameras[uid] = Camera(camera_cfg)

    # ---------------------------------------------------------------------- #
    # Reset
    # ---------------------------------------------------------------------- #
    def reset(self, seed=None):
        self.set_episode_rng(seed)
        self._elapsed_steps = 0

        self.agent.reset()

        # self.recent_sam_obs = OrderedDict()

        if self._is_ms2_env:
            obs = super().reset(seed=self._episode_seed)
        else:
            obs = self.get_obs()

        self.visualizer.reset()

        return obs

    def set_episode_rng(self, seed):
        """Set the random generator for current episode."""
        if seed is None:
            self._episode_seed = self._main_rng.randint(2**32)
        else:
            self._episode_seed = seed
        self._episode_rng = np.random.RandomState(self._episode_seed)

    # ---------------------------------------------------------------------- #
    # Methods used in simulation but do nothing in real_robot environment
    # ---------------------------------------------------------------------- #
    def reconfigure(self):
        """Reconfigure the simulation scene instance.
        This function should clear the previous scene, and create a new one.
        Left empty for real_robot
        """
        pass

    def _clear_sim_state(self):
        """Clear simulation state (velocities)
        Left empty for real_robot
        """
        pass

    def initialize_episode(self):
        """Initialize the episode, e.g., poses of actors and articulations,
            and robot configuration.
        No new assets are created.
        Task-relevant information can be initialized here, like goals.
        Left empty for real_robot
        """
        pass

    def update_render(self):
        pass

    # ---------------------------------------------------------------------- #
    # Observation
    # ---------------------------------------------------------------------- #
    @property
    def obs_mode(self):
        return self._obs_mode

    def get_obs(self):
        if self._is_ms2_env:
            return super().get_obs()

        if self._obs_mode == "none":
            # Some cases do not need observations, e.g., MPC
            return OrderedDict()
        elif self._obs_mode == "state":
            state_dict = self._get_obs_state_dict()
            return flatten_state_dict(state_dict)
        elif self._obs_mode == "state_dict":
            return self._get_obs_state_dict()
        elif self._obs_mode == "image":
            return self._get_obs_images()
        else:
            raise NotImplementedError(self._obs_mode)

    def _get_obs_state_dict(self):
        """Get (GT) state-based observations."""
        return OrderedDict(
            agent=self._get_obs_agent(),
            extra=self._get_obs_extra(),
        )

    def _get_obs_agent(self):
        obs = OrderedDict()
        obs = self.agent.get_proprioception()
        obs["base_pose"] = vectorize_pose(self.agent.robot.pose)
        return obs

    def _get_obs_extra(self) -> OrderedDict:
        # TODO: add using SAM on images for state obs
        obs = OrderedDict(
            tcp_pose=vectorize_pose(self.agent.get_tcp_pose()),
        )
        return obs

    def take_picture(self):
        """Take pictures from all cameras for syncing camera pose"""
        for cam in self._cameras.values():
            cam.take_picture()

    def get_images(self) -> Dict[str, Dict[str, np.ndarray]]:
        """Get (raw) images from all cameras"""
        images = OrderedDict()
        for name, cam in self._cameras.items():
            images[name] = cam.get_images()
        self.recent_camera_obs = images  # save for self.render()
        return images

    def get_camera_params(self) -> Dict[str, Dict[str, np.ndarray]]:
        """Get camera parameters from all cameras."""
        params = OrderedDict()
        for name, cam in self._cameras.items():
            params[name] = cam.get_params()
        return params

    def _get_obs_images(self) -> OrderedDict:
        self.update_render()
        self.take_picture()
        return OrderedDict(
            agent=self._get_obs_agent(),
            extra=self._get_obs_extra(),
            camera_param=self.get_camera_params(),
            image=self.get_images(),
        )

    # ---------------------------------------------------------------------- #
    # Reward mode
    # ---------------------------------------------------------------------- #
    @property
    def reward_mode(self):
        return self._reward_mode

    def get_reward(self, **kwargs):
        return 0.0  # TODO: update
        if self._is_ms2_env:
            return super().get_reward(**kwargs)

        if self._reward_mode == "sparse":
            eval_info = self.evaluate(**kwargs)
            return float(eval_info["success"])
        elif self._reward_mode == "dense":
            return self.compute_dense_reward(**kwargs)
        else:
            raise NotImplementedError(self._reward_mode)

    def compute_dense_reward(self, **kwargs):
        if self._is_ms2_env:
            return super().compute_dense_reward(**kwargs)

        raise NotImplementedError

    # ---------------------------------------------------------------------- #
    # Step
    # ---------------------------------------------------------------------- #
    def step(self, action: np.ndarray):
        if self._is_ms2_env:
            return super().step(action)

        self.step_action(action)
        self._elapsed_steps += 1

        obs = self.get_obs()
        info = self.get_info(obs=obs)
        reward = self.get_reward(obs=obs, action=action, info=info)
        done = self.get_done(obs=obs, info=info)

        return obs, reward, done, info

    def step_action(self, action, speed=None, mvacc=None, gripper_speed=None,
                    skip_gripper=False, wait=True):
        """
        :param action: action corresponding to self.control_mode, np.floating np.ndarray
                       action[-1] is gripper action (always has range [-1, 1])
        :param translation_scale: action [-1, 1] maps to [-100mm, 100mm],
                                  Used for delta control_mode only.
        :param axangle_scale: axangle action norm (rotation angle) is multiplied by 0.1
                              [-1, 0, 0] => rotate around [1, 0, 0] by -0.1 rad
                              Used for delta control_mode only.
        :param speed: move speed.
                      For TCP motion: range is [0.1, 1000.0] mm/s (default=100)
                      For joint motion: range is [0.05, 180.0] deg/s (default=20)
        :param mvacc: move acceleration.
                      For TCP motion: range [1.0, 50000.0] mm/s^2 (default=2000)
                      For joint motion: range [0.5, 1145.0] deg/s^2 (default=500)
        :param gripper_speed: gripper speed, range [1, 5000] r/min (default=5000)
        :param skip_gripper: whether to skip gripper action
        :param wait: whether to wait for the arm to complete, default is False.
                     Has no effect in "joint_online" and "cartesian_online" motion mode
        """
        self.agent.set_action(action,
                              translation_scale=self.action_translation_scale,
                              axangle_scale=self.action_axangle_scale,
                              speed=speed, mvacc=mvacc, gripper_speed=gripper_speed,
                              skip_gripper=skip_gripper, wait=wait)

    def evaluate(self, **kwargs) -> dict:
        """Evaluate whether the task succeeds."""
        if self._is_ms2_env:
            return super().evaluate(**kwargs)

        raise NotImplementedError

    def get_info(self, **kwargs) -> dict:
        return {}  # TODO: update
        if self._is_ms2_env:
            return super().get_info(**kwargs)

        info = dict(elapsed_steps=self._elapsed_steps)
        info.update(self.evaluate(**kwargs))
        return info

    def get_done(self, info: dict, **kwargs):
        return False  # TODO: update
        if self._is_ms2_env:
            return super().get_done(info, **kwargs)

        return bool(info["success"])

    # ---------------------------------------------------------------------- #
    # Visualization
    # ---------------------------------------------------------------------- #
    def render(self, mode="human"):
        if mode == "human":
            obs_dict = defaultdict(list)
            for cam_name, img_dict in self.recent_camera_obs.items():
                obs_dict["camera_names"].append(cam_name)
                obs_dict["color_images"].append(img_dict["rgb"])
                if "depth" in img_dict:
                    obs_dict["depth_images"].append(img_dict["depth"])

            if getattr(self, "recent_sam_obs", None) is not None:
                obs_dict["color_images"] = list(
                    self.recent_sam_obs["sam_rgb_images"]
                )
                obs_dict.pop("depth_images")  # TODO: visualize resized depth images
                obs_dict["pred_masks"] = list(
                    self.recent_sam_obs["pred_masks"]
                )
                obs_dict["xyz_images"] = list(
                    self.recent_sam_obs["sam_xyz_images"]
                )
                for object_text in self.env_object_texts:
                    obs_dict[f"{object_text}_pts"] = \
                        self.recent_sam_obs["object_pcds"][object_text]
                    obs_dict[f"{object_text}_filt_pts"] = obj_pts = \
                        self.recent_sam_obs["object_filt_pcds"][object_text]
                    obj_pts_mins, obj_pts_maxs = obj_pts.min(0), obj_pts.max(0)
                    obs_dict[f"{object_text}_bbox"] = \
                        o3d.geometry.AxisAlignedBoundingBox(
                            obj_pts_mins, obj_pts_maxs
                        )
                    obs_dict[f"{object_text}_pos_pts"] = np.mean(
                        [obj_pts_mins, obj_pts_maxs], axis=0
                    )

            self.visualizer.show_observation(**obs_dict)
            self.visualizer.render()

            #if self._obs_mode == 'state':
            #    self.visualizer.update_object(
            #        color_image=self.recent_sam_obs["color_image"],
            #        depth_image=self.recent_sam_obs["depth_image"],
            #        pred_masks=self.recent_sam_obs["pred_masks"],
            #        xyz_image=self.recent_sam_obs["world_xyz_image"],
            #        cube_pts=self.recent_valid_cube_pts,
            #        bowl_pts=self.recent_valid_bowl_pts,
            #    )
            #elif self._obs_mode == "image":
            #    self.visualizer.update_object(
            #        color_image=self.recent_sam_obs["color_image"],
            #        depth_image=self.recent_sam_obs["depth_image"],
            #    )
            #else:
            #    raise NotImplementedError()
        else:
            raise NotImplementedError(mode)
