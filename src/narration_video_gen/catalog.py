"""Loading and validating the recipe / profile catalog."""

from __future__ import annotations

import copy
from pathlib import Path

from .compat import load_yaml_file

PROFILE_REQUIRED = ("id", "platform", "recipe", "status", "requires", "evidence")
RECIPE_REQUIRED = ("id", "model_family")
# Video recipes additionally have to state what they produce; audio recipes
# (narration synthesis) have no resolution or frame rate.
RECIPE_VIDEO_REQUIRED = ("resolution", "fps", "short_test_frames")

VALID_PLATFORMS = ("linux", "windows-wsl2")
VALID_STATUS = ("recommended", "acceptable", "experimental", "not-recommended")
VALID_DURATION = ("full", "short", "gate")
VALID_GPU_EVIDENCE = ("physical", "capacity-simulated")
VALID_VISUAL = ("passed", "failed", "pending")
VALID_TIMING_SCOPE = ("e2e", "stage", "resumed-e2e")
FACE_DETAILER_OPTIONS = {
    "face-detailer-on": True,
    "face-detailer-off": False,
}
FRAME_INTERPOLATION_OPTIONS = {
    "frame-interpolation-on": True,
    "frame-interpolation-off": False,
}


class CatalogError(ValueError):
    """Raised when a recipe or profile file is malformed."""


def repo_root(start=None):
    """Walk upwards until the directory containing ``profiles/`` is found."""
    here = Path(start or __file__).resolve()
    for candidate in [here, *here.parents]:
        if (candidate / "profiles").is_dir() and (candidate / "recipes").is_dir():
            return candidate
    raise CatalogError("could not locate the repository root from %s" % here)


class Catalog:
    """All recipes and profiles, indexed by id."""

    def __init__(self, root=None, profile_dirs=None):
        self.root = Path(root) if root else repo_root()
        self.profile_dirs = [Path(path) for path in (profile_dirs or [])]
        self.recipes = {}
        self.profiles = {}
        self._load()

    # -- loading ---------------------------------------------------------

    def _load(self):
        for path in sorted((self.root / "recipes").rglob("*.yaml")):
            recipe = load_yaml_file(path)
            _require(recipe, RECIPE_REQUIRED, path)
            if recipe.get("kind", "video") == "video":
                _require(recipe, RECIPE_VIDEO_REQUIRED, path)
                short_frames = recipe["short_test_frames"]
                if (not isinstance(short_frames, int) or short_frames < 5
                        or (short_frames - 1) % 4):
                    raise CatalogError(
                        "%s: short_test_frames must be a 4n+1 integer" % path)
            if recipe["id"] in self.recipes:
                raise CatalogError("duplicate recipe id %r (%s)" % (recipe["id"], path))
            recipe["_path"] = str(path.relative_to(self.root))
            self.recipes[recipe["id"]] = recipe

        # Repository profiles must remain unique. Explicit profile directories
        # are overlays: a profile there may intentionally replace a published
        # profile with the same id for this invocation, for example a snapshot
        # that retains the source profile id. Still reject the same id from two
        # overlays because precedence between user-supplied directories would
        # otherwise be ambiguous.
        external_sources = {}
        profile_paths = [
            (path, False) for path in sorted((self.root / "profiles").rglob("*.yaml"))
        ]
        for directory in self.profile_dirs:
            if not directory.is_dir():
                raise CatalogError("profile directory does not exist: %s" % directory)
            profile_paths.extend(
                (path, True) for path in sorted(directory.rglob("*.yaml")))
        for path, external in profile_paths:
            profile = load_yaml_file(path)
            _require(profile, PROFILE_REQUIRED, path)
            try:
                profile["_path"] = str(path.relative_to(self.root))
            except ValueError:
                profile["_path"] = str(path)
            self._validate_profile(profile, path)
            if external and profile["id"] in external_sources:
                raise CatalogError(
                    "duplicate external profile id %r (%s; already loaded from %s)"
                    % (profile["id"], path, external_sources[profile["id"]]))
            if not external and profile["id"] in self.profiles:
                raise CatalogError("duplicate profile id %r (%s)" % (profile["id"], path))
            self.profiles[profile["id"]] = profile
            if external:
                external_sources[profile["id"]] = path

    def _validate_profile(self, profile, path):
        def check(field, value, allowed):
            if value not in allowed:
                raise CatalogError(
                    "%s: %s=%r must be one of %s" % (path, field, value, ", ".join(allowed))
                )

        check("platform", profile["platform"], VALID_PLATFORMS)
        check("status", profile["status"], VALID_STATUS)
        evidence = profile["evidence"] or {}
        check("evidence.duration_class", evidence.get("duration_class"), VALID_DURATION)
        check("evidence.gpu_evidence", evidence.get("gpu_evidence"), VALID_GPU_EVIDENCE)
        check("evidence.visual_review", evidence.get("visual_review"), VALID_VISUAL)
        qualified_gpu_models = evidence.get("qualified_gpu_models", [])
        if (not isinstance(qualified_gpu_models, list)
                or any(not isinstance(item, str) or not item.strip()
                       for item in qualified_gpu_models)):
            raise CatalogError(
                "%s: evidence.qualified_gpu_models must be a list of GPU model names"
                % path)
        if profile["recipe"] not in self.recipes:
            raise CatalogError(
                "%s: recipe %r is not defined under recipes/" % (path, profile["recipe"])
            )
        if not isinstance(profile.get("requires"), dict):
            raise CatalogError("%s: requires must be a mapping" % path)
        settings = profile.get("settings") or {}
        for flag in ("vae_native_upsample", "offload_transformer_before_vae_decode"):
            if flag in settings and not isinstance(settings[flag], bool):
                raise CatalogError(
                    "%s: settings.%s must be true or false" % (path, flag))
        enabled = settings.get("face_detailer_enabled")
        if enabled is not None and not isinstance(enabled, bool):
            raise CatalogError(
                "%s: settings.face_detailer_enabled must be true or false" % path)
        size = settings.get("face_detailer_size")
        if size is not None and (not isinstance(size, int) or isinstance(size, bool)
                                 or size < 16 or size % 16):
            raise CatalogError(
                "%s: settings.face_detailer_size must be a positive multiple of 16"
                % path)
        blocks = settings.get("face_detailer_blocks_to_swap")
        if blocks is not None and (not isinstance(blocks, int) or isinstance(blocks, bool)
                                   or blocks < 0 or blocks > 40):
            raise CatalogError(
                "%s: settings.face_detailer_blocks_to_swap must be between 0 and 40"
                % path)
        rife_chunk_frames = settings.get("rife_chunk_frames")
        if (rife_chunk_frames is not None
                and (not isinstance(rife_chunk_frames, int)
                     or isinstance(rife_chunk_frames, bool)
                     or rife_chunk_frames < 2)):
            raise CatalogError(
                "%s: settings.rife_chunk_frames must be an integer of at least 2"
                % path)
        self._validate_timing(profile.get("timing"), path)

    @staticmethod
    def _validate_timing(timing, path):
        """Validate optional runtime observations without making them evidence axes.

        Capacity, completion and visual review remain under ``evidence``. Timing is
        deliberately separate because the same qualified profile can have very
        different wall time under different cooling and power conditions.
        """
        if timing is None:
            return
        if not isinstance(timing, dict):
            raise CatalogError("%s: timing must be a mapping" % path)
        reference = timing.get("reference")
        observations = timing.get("observations")
        if not isinstance(reference, dict):
            raise CatalogError("%s: timing.reference must be a mapping" % path)
        if not isinstance(observations, list) or not observations:
            raise CatalogError("%s: timing.observations must be a non-empty list" % path)
        records = [("reference", reference)]
        records.extend(("observations[%d]" % index, item)
                       for index, item in enumerate(observations))
        for label, record in records:
            if not isinstance(record, dict):
                raise CatalogError("%s: timing.%s must be a mapping" % (path, label))
            seconds = record.get("wall_clock_seconds")
            if (not isinstance(seconds, (int, float)) or isinstance(seconds, bool)
                    or seconds <= 0):
                raise CatalogError(
                    "%s: timing.%s.wall_clock_seconds must be positive"
                    % (path, label))
            scope = record.get("scope")
            if scope not in VALID_TIMING_SCOPE:
                raise CatalogError(
                    "%s: timing.%s.scope=%r must be one of %s"
                    % (path, label, scope, ", ".join(VALID_TIMING_SCOPE)))
            if label == "reference" and scope != "e2e":
                raise CatalogError(
                    "%s: timing.reference.scope must be e2e because it drives ETA"
                    % path)
            if not record.get("environment"):
                raise CatalogError(
                    "%s: timing.%s.environment is required" % (path, label))

    # -- queries ---------------------------------------------------------

    def recipe_for(self, profile, options=None):
        """Return a recipe with any named, recipe-owned pipeline options applied."""
        recipe = self.recipes[profile["recipe"]]
        options = list(options or [])
        face_options = [option for option in options if option in FACE_DETAILER_OPTIONS]
        if len(face_options) > 1:
            raise CatalogError("face-detailer-on and face-detailer-off are mutually exclusive")
        interpolation_options = [
            option for option in options if option in FRAME_INTERPOLATION_OPTIONS
        ]
        if len(interpolation_options) > 1:
            raise CatalogError(
                "frame-interpolation-on and frame-interpolation-off are mutually exclusive")
        regular_options = [option for option in options
                           if option not in FACE_DETAILER_OPTIONS
                           and option not in FRAME_INTERPOLATION_OPTIONS]
        profile_face_enabled = (profile.get("settings") or {}).get(
            "face_detailer_enabled", True)
        face_enabled = (FACE_DETAILER_OPTIONS[face_options[0]]
                        if face_options else profile_face_enabled)
        interpolation_enabled = (
            FRAME_INTERPOLATION_OPTIONS[interpolation_options[0]]
            if interpolation_options else True)
        if not options and face_enabled and interpolation_enabled:
            return recipe
        resolved = copy.deepcopy(recipe)
        available = recipe.get("pipeline_options") or {}
        for option in regular_options:
            if option not in available:
                raise CatalogError(
                    "recipe %s has no pipeline option %r" % (recipe["id"], option))
            override = available[option] or {}
            if "pipeline_stages" in override:
                resolved["pipeline_stages"] = list(override["pipeline_stages"])
            if "models" in override:
                resolved["models"] = list(dict.fromkeys(
                    list(resolved.get("models") or []) + list(override["models"])))
            if "postprocess" in override:
                resolved.setdefault("postprocess", {})
                for key, value in override["postprocess"].items():
                    resolved["postprocess"][key] = copy.deepcopy(value)
        if not face_enabled:
            resolved["pipeline_stages"] = [
                stage for stage in (resolved.get("pipeline_stages") or [])
                if stage != "face-detailer"
            ]
        if not interpolation_enabled:
            resolved["pipeline_stages"] = [
                stage for stage in (resolved.get("pipeline_stages") or [])
                if stage != "rife"
            ]
            source_fps = resolved["fps"]
            resolved["output_fps"] = source_fps
            resolved.setdefault("postprocess", {}).setdefault("retime", {})[
                "target_fps"] = source_fps
        resolved["face_detailer_enabled"] = face_enabled
        resolved["frame_interpolation_enabled"] = interpolation_enabled
        resolved["active_pipeline_options"] = list(options)
        return resolved

    def by_platform(self, plat):
        return [p for p in self.profiles.values() if p["platform"] == plat]

    def sorted_profiles(self):
        return sorted(self.profiles.values(), key=lambda p: p["id"])


def _require(mapping, keys, path):
    if not isinstance(mapping, dict):
        raise CatalogError("%s: expected a mapping at the top level" % path)
    missing = [k for k in keys if k not in mapping]
    if missing:
        raise CatalogError("%s: missing required key(s): %s" % (path, ", ".join(missing)))
