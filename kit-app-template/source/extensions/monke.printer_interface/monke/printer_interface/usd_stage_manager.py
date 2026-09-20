import time
import carb
import queue
import omni

from pxr import Usd, UsdGeom
from usdrt import Usd as UsdRt, Sdf as SdfRt, UsdShade as UsdShadeRt, Gf as GfRt, Rt


class UsdStageManager:
    PRINTER_PATH = "monkeDisc://assets/AnycubicI3Mega/AnycubicI3Mega.usda"
    ANYCUBIC_PRIM_PATH_STR = "/World/Printers/AnycubicI3Mega"
    # OctoPrint pushes new telemetry roughly once a second; smooth motion between
    # samples over the same span so a new sample arrives right as the previous
    # interpolation finishes.
    SMOOTH_DURATION = 1.0

    def __init__(self, extension_queue):
        self.queue = extension_queue
        self.rt_stage : UsdRt.Stage = None
        self.pxr_stage : Usd.Stage = None
        self.attached_stage_id = None
        self.x_xfrom = None
        self.y_xfrom = None
        self.z_xfrom = None
        self.thermal_mat_path = None
        self.x_home_pos = None
        self.y_home_pos = None
        self.z_home_pos = None
        # interpolation state: value we're animating from/to, and when the
        # animation toward the current target began
        self.x_start_val = None
        self.x_target_val = None
        self.x_update_time = None
        self.y_start_val = None
        self.y_target_val = None
        self.y_update_time = None
        self.z_start_val = None
        self.z_target_val = None
        self.z_update_time = None

    def on_update(self, _event : carb.events.IEventStream):
        if not self._get_stage():
            return

        while True:
            try:
                data = self.queue.get_nowait()

            except queue.Empty:
                break

            self._on_telemetry(data)

        # runs every frame regardless of new telemetry, so motion stays smooth
        # between the low-frequency samples pushed by OctoPrint
        self._apply_position_interpolation()

    def _on_telemetry(self, data):
        # update materials
        self._upadte_thermalpad_mat(data)

        # update position targets; the actual per-frame motion is applied by
        # _apply_position_interpolation
        self._set_position_targets(data)

    def _get_stage(self):
        omni_ctx = omni.usd.get_context()
        stage_id = omni_ctx.get_stage_id()
        if not stage_id:
            print("[PrinterBridge] No USD stage found")
            return False

        # stage id is changin when user open new stage or stage get recomposed etc...
        if not self.rt_stage or stage_id != self.attached_stage_id:
            stage : Usd.Stage = omni_ctx.get_stage()
            if not stage:
                return None
            
            UsdGeom.SetStageUpAxis(stage, UsdGeom.Tokens.z)
            printer_prim : Usd.Prim = stage.DefinePrim(self.ANYCUBIC_PRIM_PATH_STR, "Xform")
            printer_prim.GetReferences().AddReference(self.PRINTER_PATH)

            self.pxr_stage = stage
            self.rt_stage = UsdRt.Stage.Attach(stage_id)
            self.attached_stage_id = stage_id

            # invalidate cached prim resolutions from the previous stage
            self.x_xfrom = None
            self.y_xfrom = None
            self.z_xfrom = None
            self.thermal_mat_path = None
            self.x_home_pos = None
            self.y_home_pos = None
            self.z_home_pos = None
            self.x_start_val = None
            self.x_target_val = None
            self.x_update_time = None
            self.y_start_val = None
            self.y_target_val = None
            self.y_update_time = None
            self.z_start_val = None
            self.z_target_val = None
            self.z_update_time = None

        if not self.x_xfrom:
            self.thermal_mat_path = SdfRt.Path(f"{self.ANYCUBIC_PRIM_PATH_STR}/Looks/thermal_heatMap/PreviewSurface")

            x_prim_path = SdfRt.Path(f"{self.ANYCUBIC_PRIM_PATH_STR}/Geom/verticalRunner/extruderHead")
            x_prim = self.rt_stage.GetPrimAtPath(x_prim_path)
            if x_prim.IsValid():
                self.x_xfrom  = Rt.Xformable(x_prim)

            y_prim_path = SdfRt.Path(f"{self.ANYCUBIC_PRIM_PATH_STR}/Geom/bed")
            y_prim = self.rt_stage.GetPrimAtPath(y_prim_path)
            if y_prim.IsValid():
                self.y_xfrom= Rt.Xformable(y_prim)

            z_prim_path = SdfRt.Path(f"{self.ANYCUBIC_PRIM_PATH_STR}/Geom/verticalRunner")
            z_prim = self.rt_stage.GetPrimAtPath(z_prim_path)
            if z_prim.IsValid():
                self.z_xfrom = Rt.Xformable(z_prim)

        return True

    def _upadte_thermalpad_mat(self, data):
        bed_temp = data.get("bed_actual")
        bed_target = data.get("bed_target", 110)    # max standard firmware bed temp
        if bed_temp:            
            new_col = self._temp_to_heatmap_rgb(bed_temp, bed_target)
            self._set_thermalpad_mat(new_col)

    def _set_thermalpad_mat(self, new_col):
        shd_prim = self.rt_stage.GetPrimAtPath(self.thermal_mat_path)
        thermal_shd  = UsdShadeRt.Shader(shd_prim)
        diffuse_color = thermal_shd.GetInput("diffuseColor")
        diffuse_color.Set(new_col)

    @staticmethod
    def _temp_to_heatmap_rgb(temp, max_temp=100.0):
        """
        Computes the thermalpad material's diffuse color based on the printer's
        current and target temperature. If no target temperature is set, we
        assume it should be 100.0.
        """
        min_temp = 20
        if max_temp <= min_temp:
            ratio = 0.0
        else:
            ratio = min(max((temp - min_temp) / (max_temp - min_temp), 0.0), 1.0)

        if ratio < 0.25:
            segment = ratio / 0.25
            r, g, b = 0.0, segment, 1.0
        elif ratio < 0.5:
            segment = (ratio - 0.25) / 0.25
            r, g, b = 0.0, 1.0, 1.0 - segment
        elif ratio < 0.75:
            segment = (ratio - 0.5) / 0.25
            r, g, b = segment, 1.0, 0.0
        else:
            segment = (ratio - 0.75) / 0.25
            r, g, b = 1.0, 1.0 - segment, 0.0

        return GfRt.Vec3f([r, g, b])

    def _get_home_pos(self, prim_path_str):
        # Read the home position via plain pxr USD instead of usdrt's Fabric-backed
        # world-position attribute: the latter is only populated once Fabric has
        # flattened a transform for this prim, which never happens for a prim with
        # no authored xformOps (confirmed: it stays permanently invalid here). This
        # one-time read isn't performance sensitive, so there's no need for the
        # Fabric fast path.
        prim = self.pxr_stage.GetPrimAtPath(prim_path_str)
        world_transform = UsdGeom.Xformable(prim).ComputeLocalToWorldTransform(Usd.TimeCode.Default())
        return world_transform.ExtractTranslation()

    def _set_position_targets(self, data):
        x = data.get("pos_x")
        y = data.get("pos_y")
        z = data.get("pos_z")

        if x is not None and self.x_xfrom:
            if self.x_home_pos is None:
                self.x_home_pos = self._get_home_pos(f"{self.ANYCUBIC_PRIM_PATH_STR}/Geom/verticalRunner/extruderHead")
            else:
                new_x_pos = (self.x_home_pos[0] - x) / 10
                self.x_start_val, self.x_target_val, self.x_update_time = self._retarget(
                    self.x_start_val, self.x_target_val, self.x_update_time, new_x_pos
                )

        if y is not None and self.y_xfrom:
            if self.y_home_pos is None:
                self.y_home_pos = self._get_home_pos(f"{self.ANYCUBIC_PRIM_PATH_STR}/Geom/bed")
            else:
                new_y_pos = (self.y_home_pos[1] + y) / 10
                self.y_start_val, self.y_target_val, self.y_update_time = self._retarget(
                    self.y_start_val, self.y_target_val, self.y_update_time, new_y_pos
                )

        if z is not None and self.z_xfrom:
            if self.z_home_pos is None:
                self.z_home_pos = self._get_home_pos(f"{self.ANYCUBIC_PRIM_PATH_STR}/Geom/verticalRunner")
            else:
                new_z_pos = (self.z_home_pos[2] + z) / 10
                self.z_start_val, self.z_target_val, self.z_update_time = self._retarget(
                    self.z_start_val, self.z_target_val, self.z_update_time, new_z_pos
                )

    def _retarget(self, start, target, start_time, new_value):
        # Begin a fresh interpolation toward new_value, starting from wherever
        # the previous interpolation currently is (not its old target) so
        # retargeting mid-motion doesn't cause a visible snap.
        current = new_value if target is None else self._current_value(start, target, start_time)
        return current, new_value, time.time()

    def _current_value(self, start, target, start_time):
        t = min((time.time() - start_time) / self.SMOOTH_DURATION, 1.0)
        return start + (target - start) * t

    def _apply_position_interpolation(self):
        if self.x_xfrom and self.x_target_val is not None:
            pos = self._current_value(self.x_start_val, self.x_target_val, self.x_update_time)
            transform_matrix = GfRt.Matrix4d().SetTranslate(GfRt.Vec3d(pos, 0.0, 0.0))
            self.x_xfrom.CreateLocalMatrixAttr(transform_matrix)

        if self.y_xfrom and self.y_target_val is not None:
            pos = self._current_value(self.y_start_val, self.y_target_val, self.y_update_time)
            transform_matrix = GfRt.Matrix4d().SetTranslate(GfRt.Vec3d(0.0, pos, 0.0))
            self.y_xfrom.CreateLocalMatrixAttr(transform_matrix)

        if self.z_xfrom and self.z_target_val is not None:
            pos = self._current_value(self.z_start_val, self.z_target_val, self.z_update_time)
            transform_matrix = GfRt.Matrix4d().SetTranslate(GfRt.Vec3d(0.0, 0.0, pos))
            self.z_xfrom.CreateLocalMatrixAttr(transform_matrix)

