import math
import random
import numpy as np
import cv2
from PIL import ImageFont, Image, ImageDraw

import libs.math_utils as math_utils
from libs.utils import draw_box, draw_bbox, prob
from libs.timer import Timer
from libs.liner import Liner
from libs.noiser import Noiser


class TextState(object):
    """
    Used in prob function, 0.03 means 3%
    """
    blur = 0.03
    prydown = 0.03

    # 带有横线效果的图片占总图片的 5%
    line = 0.1

    noise = 1


class TextEffect(object):
    blur = True
    prydown = True
    line = False
    noise = True

    def __init__(self, flags):
        self.line = flags.line
        self.noise = flags.noise


# noinspection PyMethodMayBeStatic
class Renderer(object):
    def __init__(self, corpus, fonts, bgs, texteffect, width=256, height=32, debug=False, gpu=False):
        self.corpus = corpus
        self.fonts = fonts
        self.bgs = bgs
        self.out_width = width
        self.out_height = height
        self.debug = debug
        self.gpu = gpu

        self.timer = Timer()
        self.textstate = TextState()
        self.texteffect = texteffect
        self.liner = Liner()
        self.noiser = Noiser()

    def gen_img(self):
        word = self.corpus.get_sample()

        font, word_size = self.pick_font(word)

        # Background's height should much larger than raw word image's height,
        # to make sure we can crop full word image after apply perspective
        bg = self.gen_bg(width=word_size[0] * 8, height=word_size[1] * 8)

        word_img, text_box_pnts, word_color = self.draw_text_on_bg(word, font, bg)

        if self.texteffect.line and prob(self.textstate.line):
            word_img, text_box_pnts = self.liner.apply(word_img, text_box_pnts, word_color)

        word_img, img_pnts_transformed, text_box_pnts_transformed = \
            self.apply_perspective_transform(word_img, text_box_pnts, max_x=25, max_y=25, max_z=5, gpu=self.gpu)

        if self.debug:
            word_img = draw_box(word_img, img_pnts_transformed, (0, 255, 0))
            word_img = draw_box(word_img, text_box_pnts_transformed, (0, 0, 255))
            _, crop_bbox = self.crop_img(word_img, text_box_pnts_transformed)
            word_img = draw_bbox(word_img, crop_bbox, (255, 0, 0))
        else:
            word_img, crop_bbox = self.crop_img(word_img, text_box_pnts_transformed)

        if self.texteffect.noise and prob(self.textstate.noise):
            word_img = np.clip(word_img, 0., 255.)
            word_img = self.noiser.apply(word_img)

        blured = False
        if self.texteffect.blur and prob(self.textstate.blur):
            blured = True
            word_img = self.apply_blur_on_output(word_img)

        if not blured:
            if self.texteffect.prydown and prob(self.textstate.prydown):
                word_img = self.apply_prydown(word_img)

        word_img = np.clip(word_img, 0., 255.)
        return word_img, word

    def crop_img(self, img, corner_pnts):
        """
        :param img: image to crop
        :param corner_pnts:
        :return:
            dst: image with desired output size, height=32, width=flags.img_width
            crop_bbox: bounding box on input image
        """
        bbox = cv2.boundingRect(corner_pnts)
        bbox_width = bbox[2]
        bbox_height = bbox[3]

        # 旋转的角度越大，resize 到同一个高度，文字越小
        # TODO: prevent text too small
        dst_height = random.randint(25, self.out_height)

        scale = max(bbox_height / dst_height, bbox_width / self.out_width)

        s_bbox_width = math.ceil(bbox_width / scale)
        s_bbox_height = math.ceil(bbox_height / scale)

        s_bbox = (np.around(bbox[0] / scale),
                  np.around(bbox[1] / scale),
                  np.around(bbox[2] / scale),
                  np.around(bbox[3] / scale))

        y_max_offset = 0
        if self.out_height > s_bbox_height:
            y_max_offset = self.out_height - s_bbox_height

        x_max_offset = 0
        if self.out_width > s_bbox_width:
            x_max_offset = self.out_width - s_bbox_width

        y_offset = 0
        if y_max_offset != 0:
            y_offset = random.randint(0, y_max_offset)

        x_offset = 0
        if x_max_offset != 0:
            x_offset = random.randint(0, x_max_offset)

        def int_around(val):
            return int(np.around(val))

        dst_bbox = (
            int_around((s_bbox[0] - x_offset) * scale),
            int_around((s_bbox[1] - y_offset) * scale),
            int_around(self.out_width * scale),
            int_around(self.out_height * scale)
        )

        # It's import do crop first and than do resize
        dst = img[dst_bbox[1]:dst_bbox[1] + dst_bbox[3], dst_bbox[0]:dst_bbox[0] + dst_bbox[2]]
        dst = cv2.resize(dst, (self.out_width, self.out_height), interpolation=cv2.INTER_CUBIC)

        crop_bbox = (int_around(dst_bbox[0] * scale),
                     int_around(dst_bbox[1] * scale),
                     int_around(dst_bbox[2] * scale),
                     int_around(dst_bbox[3] * scale))

        return dst, crop_bbox

    def keep_radio_scale(self, img, height):
        h = img.shape[0]
        w = img.shape[1]
        scale = h / height
        s_h = math.ceil(h / scale)
        s_w = math.ceil(w / scale)

        out = cv2.resize(img, (s_w, s_h), interpolation=cv2.INTER_AREA)
        return out

    def draw_text_on_bg(self, word, font, bg):
        """
        Draw word in the center of background
        :param word: word to draw
        :param font: font to draw word
        :param bg: background numpy image
        :return:
            np_img: word image
            text_box_pnts: left-top, right-top, right-bottom, left-bottom
        """
        bg_height = bg.shape[0]
        bg_width = bg.shape[1]

        word_size = self.get_word_size(font, word)
        word_height = word_size[1]
        word_width = word_size[0]

        offset = font.getoffset(word)

        pil_img = Image.fromarray(np.uint8(bg))
        draw = ImageDraw.Draw(pil_img)

        # Draw text in the center of bg
        text_x = int((bg_width - word_width) / 2)
        text_y = int((bg_height - word_height) / 2)

        bg_mean = int(np.mean(bg))
        word_color = random.randint(0, int(bg_mean / 3 * 2))

        draw.text((text_x - offset[0], text_y - offset[1]), word, fill=word_color, font=font)

        np_img = np.array(pil_img).astype(np.float32)

        text_box_pnts = [
            [text_x, text_y],
            [text_x + word_width, text_y],
            [text_x + word_width, text_y + word_height],
            [text_x, text_y + word_height]
        ]

        return np_img, text_box_pnts, word_color

    def gen_bg(self, width, height):
        if prob(0.5):
            bg = self.gen_rand_bg(int(width), int(height))
        else:
            bg = self.gen_bg_from_image(int(width), int(height))
        return bg

    def gen_rand_bg(self, width, height):
        """
        Generate random background
        """
        bg_high = random.uniform(220, 255)
        bg_low = bg_high - random.uniform(1, 60)

        bg = np.random.randint(bg_low, bg_high, (height, width)).astype(np.uint8)

        bg = self.apply_gauss_blur(bg)

        return bg

    # TODO: change background image resize method
    def gen_bg_from_image(self, width, height):
        bg = random.choice(self.bgs)

        out = cv2.resize(bg, (width, height))
        return out

    def pick_font(self, word):
        """
        :param word: word to generate
        :return:
            font: truetype
            size: word size, removed offset (width, height)
        """
        font_path = random.choice(self.fonts)

        # Font size in point
        font_size = random.randint(20, 30)
        font = ImageFont.truetype(font_path, font_size)

        return font, self.get_word_size(font, word)

    def get_word_size(self, font, word):
        """
        Get word size removed offset
        :param font: truetype
        :param word:
        :return:
            size: word size, removed offset (width, height)
        """
        offset = font.getoffset(word)
        size = font.getsize(word)
        size = (size[0] - offset[0], size[1] - offset[1])
        return size

    def apply_perspective_transform(self, img, text_box_pnts, max_x, max_y, max_z, gpu=False):
        """
        Apply perspective transform on image
        :param img: origin numpy image
        :param text_box_pnts: four corner points of text
        :param x: max rotate angle around X-axis
        :param y: max rotate angle around Y-axis
        :param z: max rotate angle around Z-axis
        :return:
            dst_img:
            dst_img_pnts: points of whole word image after apply perspective transform
            dst_text_pnts: points of text after apply perspective transform
        """

        x = math_utils.cliped_rand_norm(0, max_x)
        y = math_utils.cliped_rand_norm(0, max_y)
        z = math_utils.cliped_rand_norm(0, max_z)

        transformer = math_utils.PerspectiveTransform(x, y, z, scale=1.0, fovy=50)

        dst_img, M33, dst_img_pnts = transformer.transform_image(img, gpu)
        dst_text_pnts = transformer.transform_pnts(text_box_pnts, M33)

        return dst_img, dst_img_pnts, dst_text_pnts

    def apply_blur_on_output(self, img):
        if prob(0.5):
            return self.apply_gauss_blur(img, [3, 5])
        else:
            return self.apply_norm_blur(img)

    def apply_gauss_blur(self, img, ks=None):
        if ks is None:
            ks = [7, 9, 11, 13]
        ksize = random.choice(ks)

        sigmas = [0, 1, 2, 3, 4, 5, 6, 7]
        sigma = 0
        if ksize <= 3:
            sigma = random.choice(sigmas)
        img = cv2.GaussianBlur(img, (ksize, ksize), sigma)
        return img

    def apply_norm_blur(self, img, ks=None):
        # kernel == 1, the output image will be the same
        if ks is None:
            ks = [2, 3]
        kernel = random.choice(ks)
        img = cv2.blur(img, (kernel, kernel))
        return img

    def apply_prydown(self, img):
        """
        模糊图像，模拟小图片放大的效果
        """
        scale = random.uniform(1, 2.2)
        height = img.shape[0]
        width = img.shape[1]

        out = cv2.resize(img, (int(width / scale), int(height / scale)), interpolation=cv2.INTER_AREA)
        return cv2.resize(out, (width, height), interpolation=cv2.INTER_AREA)
