# T4 — Power cut (manual, cannot be scripted from inside the machine)

1. Start a fresh mock session (ramp mode) and confirm `pipeline status` shows samples growing.
2. Note the time, then cut power:
   - **Physical Pi 4:** pull the USB-C plug.
   - **QEMU:** in the monitor, `system_reset` (or kill the qemu process).
   - **libvirt:** `virsh destroy <vm>`; **Oracle/GCP:** console "Force stop" / "Reset".
3. Power on. The service starts at boot; start the mock again (new session).
4. Run `tests/chaos/after_power_cut.sh`. It exports all sessions, runs the ground-truth check,
   and prints the old session's tail loss (`to_index: null`, reason
   `device_power_loss_or_reboot`).
5. **Measured loss** = (samples the mock had produced at the cut) − (last saved index + 1).
   Estimate "produced at the cut" from the mock's elapsed time × 400. Record it against the
   stated bound (0.5 s = 200 samples per sensor) in `docs/EVIDENCE.md`.
6. Repeat 5 times; report the worst case.
