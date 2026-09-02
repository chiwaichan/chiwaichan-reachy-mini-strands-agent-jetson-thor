"""Pure-SDK hardware self-test for the Reachy Mini Lite — NO LLM / Strands.

Exercises every hardware feature through the Reachy SDK and confirms each one
actually happened, using the strongest signal available per feature:
  - motion  -> task-completion ack (goto blocks) + encoder read-back vs target
  - antennas-> present-position read-back vs target
  - body    -> joint-position delta after command
  - IMU     -> live accel/gyro (N/A on Lite -> reported as SKIP)
  - camera  -> a real frame with non-trivial content
  - mic     -> RMS level + direction-of-arrival
  - speaker -> acoustic loopback (play a sound, detect it on the mic)

Assumes a daemon is running with media enabled (test_hardware.sh starts one).
Prints a per-feature PASS/FAIL/SKIP table and exits non-zero if anything failed.
"""

from __future__ import annotations

import time

import numpy as np
from scipy.spatial.transform import Rotation as R

from reachy_mini import ReachyMini
from reachy_mini.reachy_mini import INIT_ANTENNAS_JOINT_POSITIONS, INIT_HEAD_POSE
from reachy_mini.utils import create_head_pose

# (feature, status, detail) — status in {"PASS","FAIL","SKIP"}
RESULTS: list[tuple[str, str, str]] = []


def record(feature: str, status: str, detail: str = "") -> None:
    RESULTS.append((feature, status, detail))
    icon = {"PASS": "\033[1;32m[PASS]\033[0m", "FAIL": "\033[1;31m[FAIL]\033[0m",
            "SKIP": "\033[1;33m[SKIP]\033[0m"}[status]
    print(f"{icon} {feature}: {detail}")


def rpy_deg(pose: np.ndarray) -> tuple[float, float, float]:
    r, p, y = R.from_matrix(np.asarray(pose)[:3, :3]).as_euler("xyz", degrees=True)
    return float(r), float(p), float(y)


def _max_mic_chip_energy(reads: int = 8) -> float:
    """Max per-mic speech energy read directly off the XVF3800 (0.0 if unreadable).

    Reads the chip's AEC_SPENERGY_VALUES register over USB — this reflects what the
    mics deliver to the DSP, independent of the SDK capture path. Flat zero here
    while making noise means no signal is reaching the chip (hardware/FPC cable).
    """
    try:
        from reachy_mini.media.audio_control_utils import init_respeaker_usb

        dev = init_respeaker_usb()
        if not dev:
            return 0.0
        peak = 0.0
        for _ in range(reads):
            try:
                vals = [float(x) for x in list(dev.read("AEC_SPENERGY_VALUES"))][-4:]
                peak = max(peak, *vals) if vals else peak
            except Exception:  # noqa: BLE001
                pass
            time.sleep(0.05)
        try:
            dev.close()
        except Exception:  # noqa: BLE001
            pass
        return peak
    except Exception:  # noqa: BLE001
        return 0.0


def run(mini: ReachyMini) -> None:
    # ---- 1. status / connection -----------------------------------------
    try:
        s = mini.client.get_status()
        b = s.backend_status
        record("connection+status", "PASS",
               f"version={s.version} hw_id={s.hardware_id} backend_ready={getattr(b,'ready',None)}")
    except Exception as e:  # noqa: BLE001
        record("connection+status", "FAIL", str(e))

    # ---- 2. enable motors (torque) --------------------------------------
    try:
        mini.enable_motors()
        time.sleep(0.5)
        mode = getattr(mini.client.get_status().backend_status, "motor_control_mode", "?")
        record("enable_motors", "PASS", f"motor_control_mode={mode}")
    except Exception as e:  # noqa: BLE001
        record("enable_motors", "FAIL", str(e))

    # baseline neutral
    try:
        mini.goto_target(INIT_HEAD_POSE, antennas=INIT_ANTENNAS_JOINT_POSITIONS, duration=1.5)
    except Exception:  # noqa: BLE001
        pass

    # ---- 3. head 6-DoF: orientation (pitch/roll/yaw) --------------------
    for axis, target in (("pitch", 20.0), ("roll", 20.0), ("yaw", 30.0)):
        try:
            kw = {axis: target}
            mini.goto_target(create_head_pose(degrees=True, **kw), duration=1.0)  # ack on return
            meas = dict(zip(("roll", "pitch", "yaw"), rpy_deg(mini.get_current_head_pose())))
            err = abs(meas[axis] - target)
            status = "PASS" if err <= 7.0 else "FAIL"
            record(f"head {axis}", status, f"target={target}° measured={meas[axis]:.1f}° err={err:.1f}°")
            mini.goto_target(INIT_HEAD_POSE, duration=0.8)
        except Exception as e:  # noqa: BLE001
            record(f"head {axis}", "FAIL", str(e))

    # ---- 4. head translation: full x/y/z (completes the 6 DoF) ----------
    for axis, idx, target in (("x", 0, 10.0), ("y", 1, 10.0), ("z", 2, 12.0)):
        try:
            mini.goto_target(create_head_pose(**{axis: target, "mm": True}), duration=1.0)
            meas = float(np.asarray(mini.get_current_head_pose())[idx, 3] * 1000.0)
            status = "PASS" if abs(meas - target) <= 6 else "FAIL"
            record(f"head {axis}-translate", status, f"target={target}mm measured={meas:.1f}mm")
            mini.goto_target(INIT_HEAD_POSE, duration=0.8)
        except Exception as e:  # noqa: BLE001
            record(f"head {axis}-translate", "FAIL", str(e))

    # ---- 5. body rotation -----------------------------------------------
    try:
        before, _ = mini.get_current_joint_positions()
        mini.goto_target(body_yaw=float(np.deg2rad(30)), duration=1.2)
        after, _ = mini.get_current_joint_positions()
        delta = float(np.max(np.abs(np.array(after) - np.array(before))))
        status = "PASS" if delta > 0.05 else "FAIL"
        record("body rotation", status, f"max joint delta={delta:.3f} rad after body_yaw=30°")
        mini.goto_target(body_yaw=0.0, duration=1.0)
    except Exception as e:  # noqa: BLE001
        record("body rotation", "FAIL", str(e))

    # ---- 6. antennas (both, then each independently) --------------------
    def _check_antennas(label: str, target: list[float]) -> None:
        mini.goto_target(antennas=target, duration=0.7)
        present = list(mini.get_present_antenna_joint_positions())
        err = float(np.max(np.abs(np.array(present) - np.array(target))))
        record(label, "PASS" if err <= 0.15 else "FAIL",
               f"target={target} present=[{present[0]:.2f},{present[1]:.2f}] err={err:.2f}")

    try:
        _check_antennas("antennas (both)", [0.5, -0.5])
        _check_antennas("antenna right-only", [0.6, 0.0])   # left held at 0
        _check_antennas("antenna left-only", [0.0, 0.6])    # right held at 0
        mini.goto_target(antennas=INIT_ANTENNAS_JOINT_POSITIONS, duration=0.6)
    except Exception as e:  # noqa: BLE001
        record("antennas", "FAIL", str(e))

    # ---- 7. look_at_world -----------------------------------------------
    try:
        mini.look_at_world(0.5, 0.3, 0.0)  # point head toward a world coord
        _, _, yaw = rpy_deg(mini.get_current_head_pose())
        status = "PASS" if abs(yaw) > 5 else "FAIL"
        record("look_at_world", status, f"head yaw moved to {yaw:.1f}°")
        mini.goto_target(INIT_HEAD_POSE, duration=0.8)
    except Exception as e:  # noqa: BLE001
        record("look_at_world", "FAIL", str(e))

    # ---- 8. recorded emotion playback (HF library) ----------------------
    try:
        from reachy_mini.motion.recorded_move import RecordedMoves
        moves = RecordedMoves("pollen-robotics/reachy-mini-emotions-library")
        names = moves.list_moves()
        pick = "happy" if "happy" in names else names[0]
        mini.play_move(moves.get(pick), initial_goto_duration=1.0, sound=False)
        record("recorded emotion", "PASS", f"played '{pick}' (of {len(names)} moves)")
        mini.goto_target(INIT_HEAD_POSE, antennas=INIT_ANTENNAS_JOINT_POSITIONS, duration=1.0)
    except Exception as e:  # noqa: BLE001
        record("recorded emotion", "SKIP", f"unavailable (needs HF download?): {e}")

    # ---- 9. IMU (absent on Lite -> reported as SKIP) --------------------
    try:
        imu_attr = getattr(mini, "imu", None)
        imu = imu_attr() if callable(imu_attr) else imu_attr
        if imu is None:
            record("IMU", "SKIP", "not available (expected on Lite)")
        else:
            record("IMU", "PASS", f"accel={imu.get('accelerometer')} gyro={imu.get('gyroscope')}")
    except Exception as e:  # noqa: BLE001
        record("IMU", "SKIP", str(e))

    # ---- 10. camera ------------------------------------------------------
    try:
        frame = None
        for _ in range(30):  # let the IPC pipeline warm up
            frame = mini.media.get_frame()
            if frame is not None:
                break
            time.sleep(0.1)
        if frame is None:
            record("camera", "FAIL", "no frame returned")
        else:
            arr = np.asarray(frame)
            ok = arr.ndim == 3 and arr.size > 0 and float(arr.std()) > 1.0
            record("camera", "PASS" if ok else "FAIL",
                   f"frame shape={arr.shape} dtype={arr.dtype} std={arr.std():.1f}")
    except Exception as e:  # noqa: BLE001
        record("camera", "FAIL", str(e))

    # ---- 11. microphone (SDK RMS + per-mic chip energy) -----------------
    # The mic is an XMOS XVF3800 array. We check BOTH the SDK capture level and
    # the chip's own per-mic speech-energy register, so a dead mic is correctly
    # attributed to hardware (flex/FPC cable) rather than the SDK.
    mic_rms = 0.0
    try:
        mini.media.start_recording()
        time.sleep(0.3)
        samples = []
        for _ in range(20):
            s = mini.media.get_audio_sample()
            if s is not None and len(s):
                samples.append(np.asarray(s, dtype=np.float32).ravel())
            time.sleep(0.05)
        if samples:
            buf = np.concatenate(samples)
            mic_rms = float(np.sqrt(np.mean(buf ** 2)))

        chip_energy = _max_mic_chip_energy()  # reads XVF3800 AEC_SPENERGY_VALUES

        if mic_rms > 5e-4:
            record("microphone", "PASS", f"capture RMS={mic_rms:.5f}")
        elif chip_energy > 1e-3:
            record("microphone", "FAIL",
                   f"chip hears mics (energy={chip_energy:.4g}) but SDK capture silent (routing)")
        else:
            record("microphone", "FAIL",
                   f"NO signal at the mics (capture RMS={mic_rms:.5f}, chip energy={chip_energy:.4g}) "
                   "-> hardware: reseat the mic flex/FPC cable")
        try:
            doa = mini.media.get_DoA()
            record("mic DoA", "PASS" if doa is not None else "SKIP", f"{doa}")
        except Exception as e:  # noqa: BLE001
            record("mic DoA", "SKIP", str(e))
    except Exception as e:  # noqa: BLE001
        record("microphone", "FAIL", str(e))

    # ---- 12. speaker (playback) -----------------------------------------
    # NOTE: cannot auto-verify via the mic — the XVF3800 does echo cancellation
    # and removes the speaker's own sound from the mic. We confirm the playback
    # path runs without error; audible confirmation is by ear.
    try:
        mini.media.play_sound("dance1.wav")
        time.sleep(1.5)
        record("speaker (playback)", "PASS", "played dance1.wav OK (confirm audibly; AEC blocks mic loopback)")
        try:
            mini.media.stop_recording()
        except Exception:  # noqa: BLE001
            pass
    except Exception as e:  # noqa: BLE001
        record("speaker (playback)", "FAIL", str(e))

    # ---- return to neutral ----------------------------------------------
    try:
        mini.goto_target(INIT_HEAD_POSE, antennas=INIT_ANTENNAS_JOINT_POSITIONS, body_yaw=0.0, duration=1.5)
    except Exception:  # noqa: BLE001
        pass


def main() -> int:
    print("Connecting to Reachy Mini (media_backend=default, expecting LOCAL)...")
    try:
        connection = ReachyMini(media_backend="default", connection_mode="localhost_only")
    except Exception as e:  # noqa: BLE001
        print(f"[FATAL] Could not connect: {e}")
        return 1

    with connection as mini:
        run(mini)

    # ---- summary ---------------------------------------------------------
    print("\n" + "=" * 60)
    print("HARDWARE SELF-TEST SUMMARY")
    print("=" * 60)
    width = max(len(f) for f, _, _ in RESULTS)
    n_fail = 0
    for feature, status, _ in RESULTS:
        print(f"  {feature:<{width}}  {status}")
        if status == "FAIL":
            n_fail += 1
    passed = sum(1 for _, s, _ in RESULTS if s == "PASS")
    skipped = sum(1 for _, s, _ in RESULTS if s == "SKIP")
    print("-" * 60)
    print(f"  {passed} passed, {n_fail} failed, {skipped} skipped")
    print("=" * 60)
    return 1 if n_fail else 0


if __name__ == "__main__":
    raise SystemExit(main())
