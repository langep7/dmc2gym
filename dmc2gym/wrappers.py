from gym import core, spaces
from dm_control import suite
from dm_env import specs
from dm_control.utils import rewards
import numpy as np
import os
import pandas as pd
import matplotlib.pyplot as plt

_ANGLE_BOUND = 8
_COSINE_BOUND = np.cos(np.deg2rad(_ANGLE_BOUND))
_MARGIN_BOUND = np.deg2rad(150-_ANGLE_BOUND)


def _spec_to_box(spec):
    def extract_min_max(s):
        assert s.dtype == np.float64 or s.dtype == np.float32
        dim = np.int(np.prod(s.shape))
        if type(s) == specs.Array:
            bound = np.inf * np.ones(dim, dtype=np.float32)
            return -bound, bound
        elif type(s) == specs.BoundedArray:
            zeros = np.zeros(dim, dtype=np.float32)
            return s.minimum + zeros, s.maximum + zeros

    mins, maxs = [], []
    for s in spec:
        mn, mx = extract_min_max(s)
        mins.append(mn)
        maxs.append(mx)
    low = np.concatenate(mins, axis=0)
    high = np.concatenate(maxs, axis=0)
    assert low.shape == high.shape
    return spaces.Box(low, high, dtype=np.float32)


def _flatten_obs(obs):
    obs_pieces = []
    for v in obs.values():
        flat = np.array([v]) if np.isscalar(v) else v.ravel()
        obs_pieces.append(flat)
    return np.concatenate(obs_pieces, axis=0)


class DMCWrapper(core.Env):
    def __init__(
        self,
        domain_name,
        task_name,
        task_kwargs=None,
        visualize_reward={},
        from_pixels=False,
        from_encoded_state=False,
        height=84,
        width=84,
        camera_id=0,
        frame_skip=1,
        environment_kwargs=None,
        channels_first=True,
        pos_vel_encoder=None,
        normaliser=None,
        encoded_state_dim=None,
        model=None,
        use_dense_reward=False,
        log_dir=None,
        log_pixel_images=False,
        log_measurements=False,
    ):
        assert 'random' in task_kwargs, 'please specify a seed, for deterministic behaviour'
        self._from_pixels = from_pixels
        self._from_encoded_state = from_encoded_state
        self._height = height
        self._width = width
        self._camera_id = camera_id
        self._frame_skip = frame_skip
        self._channels_first = channels_first
        self._pos_vel_encoder = pos_vel_encoder
        self._normaliser = normaliser
        self._encoded_state_dim = encoded_state_dim
        self._model = model
        self._use_dense_reward = use_dense_reward
        self._log_dir = log_dir
        self._log_pixel_images = log_pixel_images
        self._log_measurements = log_measurements

        self._log_pixel_dir_ext = '/images'

        # create task
        self._env = suite.load(
            domain_name=domain_name,
            task_name=task_name,
            task_kwargs=task_kwargs,
            visualize_reward=visualize_reward,
            environment_kwargs=environment_kwargs
        )

        # true and normalized action spaces
        self._true_action_space = _spec_to_box([self._env.action_spec()])
        self._norm_action_space = spaces.Box(
            low=-1.0,
            high=1.0,
            shape=self._true_action_space.shape,
            dtype=np.float32
        )

        # create observation space
        if from_pixels:
            shape = [3, height, width] if channels_first else [height, width, 3]
            self._observation_space = spaces.Box(
                low=0, high=255, shape=shape, dtype=np.uint8
            )
        elif from_encoded_state:
            shape = [1, encoded_state_dim]
            bound = np.inf
            self._observation_space = spaces.Box(
                low=-bound, high=bound, shape=shape, dtype=np.float32
            )
        else:
            self._observation_space = _spec_to_box(
                self._env.observation_spec().values()
            )
            
        self._state_space = _spec_to_box(
                self._env.observation_spec().values()
        )
        
        self.current_state = None

        # set seed
        self.seed(seed=task_kwargs.get('random', 1))

    def __getattr__(self, name):
        return getattr(self._env, name)

    def _get_obs(self, time_step):
        if self._from_pixels or self._from_encoded_state:
            obs = self.render(
                height=self._height,
                width=self._width,
                camera_id=self._camera_id
            )
            if self._channels_first:
                obs = obs.transpose(2, 0, 1).copy()
            if self._from_encoded_state:
                normalised_obs = self._normaliser.normalise_data([obs])
                encoded_states = self._pos_vel_encoder.get_encoded_states(normalised_obs, 1, self._model)
                return encoded_states
        else:
            obs = _flatten_obs(time_step.observation)
        return obs

    def _convert_action(self, action):
        action = action.astype(np.float64)
        true_delta = self._true_action_space.high - self._true_action_space.low
        norm_delta = self._norm_action_space.high - self._norm_action_space.low
        action = (action - self._norm_action_space.low) / norm_delta
        action = action * true_delta + self._true_action_space.low
        action = action.astype(np.float32)
        return action

    @property
    def observation_space(self):
        return self._observation_space

    @property
    def state_space(self):
        return self._state_space

    @property
    def action_space(self):
        return self._norm_action_space

    def seed(self, seed):
        self._true_action_space.seed(seed)
        self._norm_action_space.seed(seed)
        self._observation_space.seed(seed)

    def step(self, action):
        assert self._norm_action_space.contains(action)
        action = self._convert_action(action)
        assert self._true_action_space.contains(action)
        reward = 0
        extra = {'internal_state': self._env.physics.get_state().copy()}

        for _ in range(self._frame_skip):
            time_step = self._env.step(action)
            if self._use_dense_reward:
                reward += rewards.tolerance(self._env.physics.pole_vertical(), (_COSINE_BOUND, 1), margin=_MARGIN_BOUND,
                                            sigmoid='long_tail')
            else:
                reward += time_step.reward or 0
            done = time_step.last()
            if done:
                break
        obs = self._get_obs(time_step)
        self.current_state = _flatten_obs(time_step.observation)
        extra['discount'] = time_step.discount

        if self._log_pixel_images:
            self.save_pixel_img()
        if self._log_measurements:
            self.save_measurements(time_step)

        return obs, reward, done, extra

    def reset(self):
        time_step = self._env.reset()
        self.current_state = _flatten_obs(time_step.observation)
        obs = self._get_obs(time_step)
        return obs

    def render(self, mode='rgb_array', height=None, width=None, camera_id=0):
        assert mode == 'rgb_array', 'only support rgb_array mode, given %s' % mode
        height = height or self._height
        width = width or self._width
        camera_id = camera_id or self._camera_id
        return self._env.physics.render(
            height=height, width=width, camera_id=camera_id
        )

    def save_pixel_img(self):
        save_path = self._log_dir + self._log_pixel_dir_ext
        os.makedirs(save_path, exist_ok=True)

        img_number = 0
        while os.path.isfile(save_path + '/img' + str(img_number) + '.png'):
            img_number += 1

        obs = self.render(
            height=self._height,
            width=self._width,
            camera_id=self._camera_id
        )

        plt.imsave(save_path + '/img' + str(img_number) + '.png', obs)

    def save_measurements(self, time_step):
        log_local = _flatten_obs(time_step.observation)
        log_save_path = self._log_dir + '/measurements'
        if os.path.isfile(log_save_path + ".npy"):
            log = np.load(log_save_path + ".npy")
            log = np.vstack((log, log_local))
            np.save(log_save_path, log)
            df_log = pd.DataFrame(log)
            df_log.to_csv(log_save_path + ".csv")
        else:
            np.save(log_save_path, log_local)



