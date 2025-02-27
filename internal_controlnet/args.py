from __future__ import annotations
import os
import torch
import numpy as np
from typing import Optional, List, Annotated, ClassVar, Callable, Any, Tuple, Union, Dict
from pydantic import BaseModel, validator, root_validator, Field, field_validator, ConfigDict, model_validator, model_serializer
from PIL import Image
from logging import Logger
from copy import copy
from enum import Enum

from scripts.enums import (
    InputMode,
    ResizeMode,
    ControlMode,
    HiResFixOption,
    PuLIDMode,
    ControlNetUnionControlType,
)
from annotator.util import HWC3


def _unimplemented_func(*args, **kwargs):
    raise NotImplementedError("Not implemented.")


def field_to_displaytext(fieldname: str) -> str:
    return " ".join([word.capitalize() for word in fieldname.split("_")])


def displaytext_to_field(text: str) -> str:
    return "_".join([word.lower() for word in text.split(" ")])


def serialize_value(value) -> str:
    if isinstance(value, Enum):
        return value.value
    return str(value)


def parse_value(value: str) -> Union[str, float, int, bool]:
    if value in ("True", "False"):
        return value == "True"
    try:
        return int(value)
    except ValueError:
        try:
            return float(value)
        except ValueError:
            return value  # Plain string.

class ControlNetUnit(BaseModel):
    """
    Represents an entire ControlNet processing unit.
    """

    ext_compat_keys: ClassVar[Dict[str, str]] = {
        'guessmode': 'guess_mode',
        'guidance': 'guidance_end',
        'lowvram': 'low_vram',
        # Другие пары alias
    }

    model_config = ConfigDict(
        arbitrary_types_allowed=True,
        extra="ignore"
    )   

    # Пример обновления классовых атрибутов
    cls_match_module: ClassVar[Callable[[str], bool]] = _unimplemented_func
    cls_match_model: ClassVar[Callable[[str], bool]] = _unimplemented_func
    cls_decode_base64: ClassVar[Callable[[str], np.ndarray]] = _unimplemented_func
    cls_torch_load_base64: ClassVar[Callable[[Any], torch.Tensor]] = _unimplemented_func
    cls_get_preprocessor: ClassVar[Callable[[str], Any]] = _unimplemented_func
    cls_logger: ClassVar[Logger] = Logger("ControlNetUnit")

    # Поля UI
    is_ui: bool = False
    input_mode: InputMode = InputMode.SIMPLE
    batch_images: Optional[Any] = None
    output_dir: str = ""
    loopback: bool = False

    # Общие поля
    enabled: bool = False
    module: str = "none"

    @field_validator("module")
    @classmethod
    def check_module(cls, value: str) -> str:
        if not ControlNetUnit.cls_match_module(value):
            raise ValueError(f"module({value}) not found in supported modules.")
        return value

    model: str = "None"

   # Валидатор для поля "module"
    @field_validator("module", mode="before")
    @classmethod
    def check_module(cls, value: str) -> str:
        if not ControlNetUnit.cls_match_module(value):
            raise ValueError(f"module({value}) not found in supported modules.")
        return value

    model: str = "None"

    @field_validator("model", mode="before")
    @classmethod
    def check_model(cls, value: str) -> str:
        if not ControlNetUnit.cls_match_model(value):
            raise ValueError(f"model({value}) not found in supported models.")
        return value

    weight: Annotated[float, Field(ge=0.0, le=2.0)] = 1.0

    image: Optional[Any] = None
    resize_mode: ResizeMode = ResizeMode.INNER_FIT


    @field_validator("resize_mode", mode="before")
    @classmethod
    def check_resize_mode(cls, value) -> ResizeMode:
        resize_mode_aliases = {
            "Inner Fit (Scale to Fit)": "Crop and Resize",
            "Outer Fit (Shrink to Fit)": "Resize and Fill",
            "Scale to Fit (Inner Fit)": "Crop and Resize",
            "Envelope (Outer Fit)": "Resize and Fill",
        }
        if isinstance(value, str):
            return ResizeMode(resize_mode_aliases.get(value, value))
        assert isinstance(value, ResizeMode)
        return value

    low_vram: bool = False
    processor_res: int = -1
    threshold_a: float = -1
    threshold_b: float = -1


    @model_validator(mode="before")
    def bound_check_params(cls, values: dict) -> dict:
        """
        Проверяет и корректирует негативные параметры в 'unit'.
        """
        enabled = values.get("enabled")
        if not enabled:
            return values

        module = values.get("module")
        if not module:
            return values

        preprocessor = cls.cls_get_preprocessor(module)
        assert preprocessor is not None
        for unit_param, param in zip(
            ("processor_res", "threshold_a", "threshold_b"),
            ("slider_resolution", "slider_1", "slider_2"),
        ):
            value = values.get(unit_param)
            cfg = getattr(preprocessor, param)
            if value < cfg.minimum or value > cfg.maximum:
                values[unit_param] = cfg.value
                if value != -1:
                    cls.cls_logger.info(
                        f"[{module}.{unit_param}] Invalid value({value}), using default value {cfg.value}."
                    )
        return values

    guidance_start: Annotated[float, Field(ge=0.0, le=1.0)] = 0.0
    guidance_end: Annotated[float, Field(ge=0.0, le=1.0)] = 1.0

    @model_validator(mode="before")
    def guidance_check(cls, values: dict) -> dict:
        start = values.get("guidance_start")
        end = values.get("guidance_end")
        if start is not None and end is not None and start > end:
            raise ValueError(f"guidance_start({start}) > guidance_end({end})")
        return values

    pixel_perfect: bool = False
    control_mode: ControlMode = ControlMode.BALANCED
    # Whether to crop input image based on A1111 img2img mask. This flag is only used when `inpaint area`
    # in A1111 is set to `Only masked`. In API, this correspond to `inpaint_full_res = True`.
    inpaint_crop_input_image: bool = True
    # If hires fix is enabled in A1111, how should this ControlNet unit be applied.
    # The value is ignored if the generation is not using hires fix.
    hr_option: HiResFixOption = HiResFixOption.BOTH

    # Whether save the detected map of this unit. Setting this option to False prevents saving the
    # detected map or sending detected map along with generated images via API.
    # Currently the option is only accessible in API calls.
    save_detected_map: bool = True

    # Weight for each layer of ControlNet params.
    # For ControlNet:
    # - SD1.5: 13 weights (4 encoder block * 3 + 1 middle block)
    # - SDXL: 10 weights (3 encoder block * 3 + 1 middle block)
    # For T2IAdapter
    # - SD1.5: 5 weights (4 encoder block + 1 middle block)
    # - SDXL: 4 weights (3 encoder block + 1 middle block)
    # For IPAdapter
    # - SD15: 16 (6 input blocks + 9 output blocks + 1 middle block)
    # - SDXL: 11 weights (4 input blocks + 6 output blocks + 1 middle block)
    # Note1: Setting advanced weighting will disable `soft_injection`, i.e.
    # It is recommended to set ControlMode = BALANCED when using `advanced_weighting`.
    # Note2: The field `weight` is still used in some places, e.g. reference_only,
    # even advanced_weighting is set.
    advanced_weighting: Optional[List[float]] = None

    # The effective region mask that unit's effect should be restricted to.
    effective_region_mask: Optional[np.ndarray] = None

    @field_validator("effective_region_mask", mode="before")
    @classmethod
    def parse_effective_region_mask(cls, value) -> np.ndarray:
        if isinstance(value, str):
            return cls.cls_decode_base64(value)
        assert isinstance(value, np.ndarray) or value is None
        return value

    # The weight mode for PuLID.
    # https://github.com/ToTheBeginning/PuLID
    pulid_mode: PuLIDMode = PuLIDMode.FIDELITY

    # ControlNet control type for ControlNet union model.
    # https://github.com/xinsir6/ControlNetPlus/tree/main
    # The value of this field is only used when the model is ControlNetUnion.
    union_control_type: ControlNetUnionControlType = ControlNetUnionControlType.UNKNOWN

    # ------- API only fields -------
    # The tensor input for ipadapter. When this field is set in the API,
    # the base64string will be interpret by torch.load to reconstruct ipadapter
    # preprocessor output.
    # Currently the option is only accessible in API calls.
    ipadapter_input: Optional[List[Any]] = None

    @field_validator("ipadapter_input", mode="before")
    @classmethod
    def parse_ipadapter_input(cls, value) -> Optional[List[Any]]:
        if value is None:
            return None
        if isinstance(value, str):
            value = [value]
        result = [cls.cls_torch_load_base64(b) for b in value]
        assert result, "input cannot be empty"
        return result

    # The mask to be used on top of the image.
    mask: Optional[Any] = None

    # AnimateDiff compatibility fields.
    # TODO: Find a better way in AnimateDiff to deal with these extra fields.
    batch_mask_dir: Optional[str] = None
    animatediff_batch: bool = False
    batch_modifiers: list = Field(default_factory=list)
    batch_image_files: list = Field(default_factory=list)
    batch_keyframe_idx: Optional[str | list] = None

    @property
    def accepts_multiple_inputs(self) -> bool:
        """This unit can accept multiple input images."""
        return self.is_ipadapter

    @property
    def is_animate_diff_batch(self) -> bool:
        return getattr(self, "animatediff_batch", False)

    @property
    def uses_clip(self) -> bool:
        """Whether this unit uses clip preprocessor."""
        return any(
            (
                ("ip-adapter" in self.module and "face_id" not in self.module),
                self.module
                in ("clip_vision", "revision_clipvision", "revision_ignore_prompt"),
            )
        )

    @property
    def is_inpaint(self) -> bool:
        return "inpaint" in self.module

    @property
    def is_ipadapter(self) -> bool:
        p = ControlNetUnit.cls_get_preprocessor(self.module)
        if p is None:
            return False
        return "IP-Adapter" in p.tags

    def get_actual_preprocessors(self) -> List[Any]:
        p = ControlNetUnit.cls_get_preprocessor(self.module)
        # Map "ip-adapter-auto" to actual preprocessor.
        if self.module == "ip-adapter-auto":
            p = p.get_preprocessor_by_model(self.model)

        # Add all dependencies.
        return [p] + [
            ControlNetUnit.cls_get_preprocessor(dep) for dep in p.preprocessor_deps
        ]

    @classmethod
    def parse_image(cls, image) -> np.ndarray:
        if isinstance(image, np.ndarray):
            np_image = image
        elif isinstance(image, str):
            # Necessary for batch.
            if os.path.exists(image):
                np_image = np.array(Image.open(image)).astype("uint8")
            else:
                np_image = cls.cls_decode_base64(image)
        else:
            raise ValueError(f"Unrecognized image format {image}.")

        # Convert following image shapes to shape [H, W, C=3].
        # - [H, W]
        # - [H, W, 1]
        # - [H, W, 4]
        np_image = HWC3(np_image)
        assert np_image.ndim == 3
        assert np_image.shape[2] == 3
        return np_image

    @classmethod
    def combine_image_and_mask(
        cls, np_image: np.ndarray, np_mask: Optional[np.ndarray] = None
    ) -> np.ndarray:
        """RGB + Alpha(Optional) => RGBA"""
        # TODO: Change protocol to use 255 as A channel value.
        # Note: mask is by default zeros, as both inpaint and
        # clip mask does extra work on masked area.
        np_mask = (np.zeros_like(np_image) if np_mask is None else np_mask)[:, :, 0:1]
        if np_image.shape[:2] != np_mask.shape[:2]:
            raise ValueError(
                f"image shape ({np_image.shape[:2]}) not aligned with mask shape ({np_mask.shape[:2]})"
            )
        return np.concatenate([np_image, np_mask], axis=2)  # [H, W, 4]

    @model_validator(mode="before")
    @classmethod
    def legacy_field_alias(cls, values: Dict[str, Any]) -> Dict[str, Any]:
        for alias, key in cls.ext_compat_keys.items():
            if alias in values:
                if key in values:
                    raise ValueError(f"Conflict of field '{alias}' and '{key}'")
                values[key] = values.pop(alias)
                print(f"Deprecated alias '{alias}' detected. Use '{key}' instead.")
        return values

    @model_validator(mode="before")
    @classmethod
    def mask_alias(cls, values: Dict[str, Any]) -> Dict[str, Any]:
        mask_image = values.get("mask_image")
        mask = values.get("mask")
        if mask_image is not None:
            if mask is not None:
                raise ValueError("Cannot specify both 'mask' and 'mask_image'!")
            values["mask"] = mask_image
        return values

    def get_input_images_rgba(self) -> Optional[List[np.ndarray]]:
        init_image = self.image
        init_mask = self.mask

        if init_image is None:
            assert init_mask is None
            return None

        if isinstance(init_image, (list, tuple)):
            if not init_image:
                raise ValueError(f"{init_image} is not a valid 'image' field value")
            if isinstance(init_image[0], dict):
                images = init_image
            else:
                assert len(init_image) == 2
                images = [{"image": init_image[0], "mask": init_image[1]}]
        elif isinstance(init_image, dict):
            images = [init_image]
        elif isinstance(init_image, (str, np.ndarray)):
            images = [{"image": init_image, "mask": init_mask}]
        else:
            raise ValueError(f"Unrecognized image field {init_image}")

        np_images = []
        for image_dict in images:
            assert isinstance(image_dict, dict)
            image = image_dict.get("image")
            mask = image_dict.get("mask")
            assert image is not None

            np_image = self.parse_image(image)
            np_mask = self.parse_image(mask) if mask is not None else None
            np_images.append(self.combine_image_and_mask(np_image, np_mask))
        return np_images

    @classmethod
    def from_dict(cls, values: Dict[str, Any]) -> "ControlNetUnit":
        return cls.model_validate(values)

    @classmethod
    def from_infotext_args(cls, *args) -> "ControlNetUnit":
        assert len(args) == len(cls.infotext_fields())
        return cls.from_dict({k: v for k, v in zip(cls.infotext_fields(), args)})

    @staticmethod
    def infotext_fields() -> Tuple[str, ...]:
        return (
            "module", 
            "model", 
            "weight", 
            "resize_mode", 
            "processor_res", 
            "threshold_a", 
            "threshold_b", 
            "guidance_start", 
            "guidance_end", 
            "pixel_perfect", 
            "control_mode"
        )

    @model_serializer()
    def serialize(self) -> str:
        infotext_dict = {
            field: str(getattr(self, field)) for field in self.infotext_fields()
        }
        return ", ".join(f"{key}: {value}" for key, value in infotext_dict.items())

    @classmethod
    def parse(cls, text: str) -> "ControlNetUnit":
        return cls(
            **{
                key.strip(): value.strip()
                for item in text.split(",")
                for (key, value) in (item.split(": ", 1),)
            }
        )

    def __copy__(self) -> "ControlNetUnit":
        return self.model_copy()
