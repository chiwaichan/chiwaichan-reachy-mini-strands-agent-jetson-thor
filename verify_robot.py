"""Read-only verification that the Reachy SDK can talk to the connected robot.

Assumes a daemon is already running on localhost:8000 (verify_robot.sh starts one).
Connects via the Python SDK with media disabled and only READS state — it does not
move any motor, wake, or sleep the robot.
"""

from __future__ import annotations

import sys

import numpy as np

from reachy_mini import ReachyMini


def main() -> int:
    print("[..] Connecting to daemon via Reachy SDK (media disabled, read-only)...")
    try:
        mini = ReachyMini(media_backend="no_media", connection_mode="localhost_only")
    except Exception as e:  # noqa: BLE001
        print(f"[fail] SDK could not connect to the daemon: {e}")
        return 1

    with mini:
        # 1. Daemon/backend status — proves the daemon is bound to the hardware.
        status = mini.client.get_status()
        print(f"[ok] SDK connected. robot_name={status.robot_name}")
        print(f"     wireless_version={status.wireless_version}  version={status.version}")
        print(f"     state={status.state}  hardware_id={status.hardware_id}")
        backend = status.backend_status
        if backend is None:
            print("[warn] No backend_status — daemon is up but not bound to a robot backend.")
        else:
            ready = getattr(backend, "ready", None)
            print(f"     backend.ready={ready}  motor_control_mode={getattr(backend, 'motor_control_mode', '?')}")
            print(f"     backend.last_alive={getattr(backend, 'last_alive', '?')}  error={getattr(backend, 'error', None)}")

        # 2. Read live joint feedback — proves the SDK round-trips to the motors.
        try:
            head_joints, antennas = mini.get_current_joint_positions()
            present_antennas = mini.get_present_antenna_joint_positions()
            head_pose = mini.get_current_head_pose()
            print("[ok] Live joint feedback read from hardware:")
            print(f"     head joint positions (rad): {np.round(head_joints, 4).tolist()}")
            print(f"     antenna targets (rad):       {np.round(antennas, 4).tolist()}")
            print(f"     antenna present (rad):       {np.round(present_antennas, 4).tolist()}")
            print(f"     head pose matrix shape:      {np.asarray(head_pose).shape}")
        except Exception as e:  # noqa: BLE001
            print(f"[warn] Connected to daemon, but reading joint feedback failed: {e}")
            print("       The control board is reachable, but motors may be unpowered")
            print("       (connect wall power) or the backend isn't ready yet.")
            return 2

    print("\n[done] SDK <-> daemon <-> robot chain verified. No motors were moved.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
