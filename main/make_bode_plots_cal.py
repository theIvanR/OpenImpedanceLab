import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Tuple

import numpy as np
import pandas as pd
import sounddevice as sd
from scipy import signal as sig
import matplotlib.pyplot as plt


"""
A Bode plotter using a sound card to measure impedance via a voltage divider.

Wiring (normal measurement):
    OUT -> R_sense -> node A -> DUT -> GND
    Input channel 0 measures node before R_sense (source side)
    Input channel 1 measures node A (DUT side)

Calibration workflow:
    1) Channel / loopback: both inputs on the same node.
    2) Open: DUT disconnected.
    3) Short: DUT shorted.

If a .cal file exists, it is loaded automatically.
If not, ensure_calibration() will prompt and create it right then and there.
"""


@dataclass
class CalibrationData:
    fs: int
    nperseg: int
    noverlap: int
    f_min: float
    f_max: float
    created_utc: str
    channel_freqs: Optional[np.ndarray] = None
    channel_tf: Optional[np.ndarray] = None
    open_freqs: Optional[np.ndarray] = None
    open_tf: Optional[np.ndarray] = None
    short_freqs: Optional[np.ndarray] = None
    short_tf: Optional[np.ndarray] = None


class BodePlotter:

    def __init__(
        self,
        input_device: int,
        output_device: int,
        fs: int,
        duration_s: float,
        output_rms: float,
        r_sense: float,
        nperseg: int,
        f_min: float,
        f_max: float,
        noverlap: Optional[int] = None,
        cal_file: Optional[str] = None,
        auto_load_calibration: bool = True,
    ):
        self.input_device = input_device
        self.output_device = output_device
        self.fs = fs
        self.duration_s = duration_s
        self.output_rms = output_rms
        self.r_sense = r_sense
        self.nperseg = nperseg
        self.noverlap = noverlap if noverlap is not None else nperseg // 2
        self.f_min = f_min
        self.f_max = f_max

        self.cal_file = cal_file
        self.cal: Optional[CalibrationData] = None
        if cal_file and auto_load_calibration and Path(cal_file).exists():
            self.load_calibration(cal_file)
        elif cal_file and auto_load_calibration:
            print(f"Calibration file not found yet: {cal_file}")

        self.stimulus: Optional[np.ndarray] = None
        self.recording: Optional[np.ndarray] = None
        self.f: Optional[np.ndarray] = None
        self.H_div: Optional[np.ndarray] = None
        self.coherence: Optional[np.ndarray] = None
        self.Z: Optional[np.ndarray] = None

    def _check_devices(self):
        """Verify that selected devices exist and support required channels."""
        try:
            sd.query_devices(self.input_device)
            sd.query_devices(self.output_device)
        except sd.PortAudioError as e:
            raise RuntimeError(f"Invalid device index: {e}")

        in_dev = sd.query_devices(self.input_device, 'input')
        out_dev = sd.query_devices(self.output_device, 'output')
        if in_dev['max_input_channels'] < 2:
            raise ValueError(
                f"Input device {self.input_device} has only {in_dev['max_input_channels']} input channels; need at least 2."
            )
        if out_dev['max_output_channels'] < 1:
            raise ValueError(f"Output device {self.output_device} has no output channels.")

    def _generate_stimulus(self, seed: int = 12345, fade_s: float = 0.05) -> np.ndarray:
        """Generate FFT-shaped band-limited noise stimulus."""
        n = int(self.duration_s * self.fs)
        if n <= 0:
            raise ValueError("duration_s must be positive.")

        rng = np.random.default_rng(seed)
        x = rng.standard_normal(n)
        x = x / (np.sqrt(np.mean(x**2)) + 1e-15) * self.output_rms

        fade_n = int(fade_s * self.fs)
        if fade_n > 0 and 2 * fade_n <= n:
            fade = 0.5 - 0.5 * np.cos(np.linspace(0, np.pi, fade_n))
            x[:fade_n] *= fade
            x[-fade_n:] *= fade[::-1]

        X = np.fft.rfft(x)
        f = np.fft.rfftfreq(len(x), d=1.0 / self.fs)
        mask = (f >= self.f_min) & (f <= self.f_max)
        X *= mask.astype(X.dtype)
        x = np.fft.irfft(X, n=len(x))
        x = x / (np.sqrt(np.mean(x**2)) + 1e-15) * self.output_rms
        return x.astype(np.float64)

    def _record(self):
        """Play stimulus and record two input channels."""
        sd.default.device = (self.input_device, self.output_device)
        sd.default.samplerate = self.fs
        self.stimulus = self._generate_stimulus()
        rec = sd.playrec(
            self.stimulus.reshape(-1, 1),
            samplerate=self.fs,
            channels=2,
            dtype='float64',
        )
        sd.wait()
        self.recording = rec

    def _estimate_transfer_from_recording(self) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
        """Estimate H = V_dut / V_src and coherence from the current recording."""
        if self.recording is None:
            raise RuntimeError("No recording available. Call _record() first.")

        v_src = self.recording[:, 0] - np.mean(self.recording[:, 0])
        v_dut = self.recording[:, 1] - np.mean(self.recording[:, 1])

        f, P_yx = sig.csd(
            v_dut, v_src,
            fs=self.fs,
            window='hann',
            nperseg=self.nperseg,
            noverlap=self.noverlap,
            detrend=False,
            scaling='density'
        )
        _, P_xx = sig.welch(
            v_src,
            fs=self.fs,
            window='hann',
            nperseg=self.nperseg,
            noverlap=self.noverlap,
            detrend=False,
            scaling='density'
        )
        _, coh = sig.coherence(
            v_dut, v_src,
            fs=self.fs,
            window='hann',
            nperseg=self.nperseg,
            noverlap=self.noverlap,
            detrend=False
        )

        H = np.conj(P_yx) / (P_xx + 1e-30)
        mask = (f >= self.f_min) & (f <= self.f_max)
        return f[mask], H[mask], coh[mask]

    def _measure_once(self) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
        self._record()
        return self._estimate_transfer_from_recording()

    def save_calibration(self, filename: Optional[str] = None):
        """Save the current calibration into a .cal file."""
        if self.cal is None:
            raise RuntimeError("No calibration loaded or generated.")

        filename = filename or self.cal_file
        if filename is None:
            raise ValueError("No calibration filename provided.")

        path = Path(filename)
        payload = {
            "meta": {
                "fs": self.cal.fs,
                "nperseg": self.cal.nperseg,
                "noverlap": self.cal.noverlap,
                "f_min": self.cal.f_min,
                "f_max": self.cal.f_max,
                "created_utc": self.cal.created_utc,
            },
            "channel_freqs": self.cal.channel_freqs,
            "channel_tf_real": None if self.cal.channel_tf is None else np.real(self.cal.channel_tf),
            "channel_tf_imag": None if self.cal.channel_tf is None else np.imag(self.cal.channel_tf),
            "open_freqs": self.cal.open_freqs,
            "open_tf_real": None if self.cal.open_tf is None else np.real(self.cal.open_tf),
            "open_tf_imag": None if self.cal.open_tf is None else np.imag(self.cal.open_tf),
            "short_freqs": self.cal.short_freqs,
            "short_tf_real": None if self.cal.short_tf is None else np.real(self.cal.short_tf),
            "short_tf_imag": None if self.cal.short_tf is None else np.imag(self.cal.short_tf),
        }

        with path.open("wb") as f:
            np.savez_compressed(f, **payload)

        self.cal_file = str(path)
        print(f"Saved calibration -> {path}")

    def load_calibration(self, filename: str):
        """Load calibration from a .cal file."""
        path = Path(filename)
        if not path.exists():
            raise FileNotFoundError(f"Calibration file not found: {path}")

        with path.open("rb") as f:
            data = np.load(f, allow_pickle=True)
            meta = data["meta"].item()
            cal = CalibrationData(
                fs=int(meta["fs"]),
                nperseg=int(meta["nperseg"]),
                noverlap=int(meta["noverlap"]),
                f_min=float(meta["f_min"]),
                f_max=float(meta["f_max"]),
                created_utc=str(meta.get("created_utc", "")),
            )

            def _rebuild(freq_key: str, real_key: str, imag_key: str):
                freqs = data[freq_key] if freq_key in data.files else None
                real = data[real_key] if real_key in data.files else None
                imag = data[imag_key] if imag_key in data.files else None
                if freqs is None or real is None or imag is None:
                    return None, None
                return freqs, real + 1j * imag

            cal.channel_freqs, cal.channel_tf = _rebuild("channel_freqs", "channel_tf_real", "channel_tf_imag")
            cal.open_freqs, cal.open_tf = _rebuild("open_freqs", "open_tf_real", "open_tf_imag")
            cal.short_freqs, cal.short_tf = _rebuild("short_freqs", "short_tf_real", "short_tf_imag")

        self.cal = cal
        self.cal_file = str(path)
        print(f"Loaded calibration <- {path}")

    @staticmethod
    def _interp_complex(freq_src: np.ndarray, tf_src: np.ndarray, freq_dst: np.ndarray) -> np.ndarray:
        """Interpolate a complex transfer function onto a target frequency grid."""
        if freq_src is None or tf_src is None:
            return np.ones_like(freq_dst, dtype=np.complex128)
        re = np.interp(freq_dst, freq_src, np.real(tf_src), left=np.nan, right=np.nan)
        im = np.interp(freq_dst, freq_src, np.imag(tf_src), left=np.nan, right=np.nan)
        return re + 1j * im

    def _apply_calibration(self, f: np.ndarray, H_raw: np.ndarray) -> np.ndarray:
        """Apply channel calibration + open/short affine correction if available."""
        if self.cal is None:
            return H_raw

        H = H_raw.astype(np.complex128)

        if self.cal.channel_tf is not None and self.cal.channel_freqs is not None:
            H_chan = self._interp_complex(self.cal.channel_freqs, self.cal.channel_tf, f)
            H = H / (H_chan + 1e-30)

        if (
            self.cal.open_tf is not None and self.cal.open_freqs is not None and
            self.cal.short_tf is not None and self.cal.short_freqs is not None
        ):
            H_open = self._interp_complex(self.cal.open_freqs, self.cal.open_tf, f)
            H_short = self._interp_complex(self.cal.short_freqs, self.cal.short_tf, f)
            den = H_open - H_short
            H = np.where(np.abs(den) > 1e-12, (H - H_short) / den, np.nan + 1j * np.nan)

        return H

    def _estimate_transfer(self):
        """Measure transfer function and apply calibration if present."""
        f, H_raw, coh = self._measure_once()
        self.f = f
        self.H_div = self._apply_calibration(f, H_raw)
        self.coherence = coh

    def _compute_impedance(self):
        """Compute impedance from voltage divider equation."""
        if self.H_div is None:
            raise RuntimeError("No transfer function available. Call _estimate_transfer() first.")

        den = 1.0 - self.H_div
        Z = np.where(np.abs(den) > 1e-12, self.r_sense * self.H_div / den, np.nan + 1j * np.nan)
        self.Z = Z

    def run(self):
        """Run the whole measurement process."""
        self._check_devices()
        self._estimate_transfer()
        self._compute_impedance()

    def calibrate(
        self,
        filename: Optional[str] = None,
        prompt_user: bool = True,
        settle_s: float = 2.0,
        do_channel: bool = True,
        do_open: bool = True,
        do_short: bool = True,
    ):
        """Measure calibration states and save them to a .cal file."""
        self._check_devices()
    
        cal = CalibrationData(
            fs=self.fs,
            nperseg=self.nperseg,
            noverlap=self.noverlap,
            f_min=self.f_min,
            f_max=self.f_max,
            created_utc=datetime.now(timezone.utc).isoformat(),
        )
    
        def _prompt(msg: str):
            if prompt_user:
                print("\n" + msg)
                input("→ Press Enter to start measurement...")
                time.sleep(0.5)  # small settle time
            else:
                print("\n" + msg)
                time.sleep(settle_s)
    
        if do_channel:
            _prompt(
                "Calibration 1/3: CHANNEL / LOOPBACK"
                "\nConnect BOTH input channels to the SAME node."
            )
            print("Measuring...")
            f, H, _ = self._measure_once()
            print("Done.")
            cal.channel_freqs = f
            cal.channel_tf = H
    
        if do_open:
            _prompt(
                "Calibration 2/3: OPEN"
                "\nLeave the DUT OPEN (disconnected)."
            )
            print("Measuring...")
            f, H, _ = self._measure_once()
            print("Done.")
            cal.open_freqs = f
            cal.open_tf = H
    
        if do_short:
            _prompt(
                "Calibration 3/3: SHORT"
                "\nSHORT the DUT terminals."
            )
            print("Measuring...")
            f, H, _ = self._measure_once()
            print("Done.")
            cal.short_freqs = f
            cal.short_tf = H
    
        self.cal = cal
        self.save_calibration(filename or self.cal_file or "impedance_bode.cal")
    
        # ✅ Final confirmation before proceeding to measurement
        if prompt_user:
            print("\n✅ Calibration complete.")
            print("System is ready for measurement.")
            input("→ Press Enter to run measurement...")
            time.sleep(0.5)

    def ensure_calibration(
        self,
        filename: Optional[str] = None,
        prompt_user: bool = True,
        settle_s: float = 2.0,
        do_channel: bool = True,
        do_open: bool = True,
        do_short: bool = True,
    ):
        """Load an existing calibration if present; otherwise create one now."""
        filename = filename or self.cal_file or "impedance_bode.cal"
        path = Path(filename)

        if path.exists():
            self.load_calibration(str(path))
        else:
            self.calibrate(
                filename=str(path),
                prompt_user=prompt_user,
                settle_s=settle_s,
                do_channel=do_channel,
                do_open=do_open,
                do_short=do_short,
            )

    def save(self, filename: str = "impedance_bode", save_csv: bool = True, save_plot: bool = True):
        """Save results as CSV and/or plot."""
        if self.Z is None:
            raise RuntimeError("No impedance data available. Call run() first.")

        freq = self.f
        Z_mag = np.abs(self.Z)
        Z_phase = np.rad2deg(np.angle(self.Z))
        coh = self.coherence

        if save_csv:
            df = pd.DataFrame({
                'frequency_hz': freq,
                'Z_mag_ohm': Z_mag,
                'Z_phase_deg': Z_phase,
                'Coherence': coh,
            })
            csv_name = f"{filename}.csv"
            df.to_csv(csv_name, index=False)
            print(f"Saved CSV -> {csv_name}")

        if save_plot:
            fig, (ax1, ax2, ax3) = plt.subplots(3, 1, figsize=(10, 10), sharex=True)
            ax1.semilogx(freq, np.abs(self.Z))
            ax1.set_ylabel('Impedance (Ω)')
            ax1.grid(True, which='both', linestyle=':')
            ax1.set_title('Measured Impedance')

            ax2.semilogx(freq, np.rad2deg(np.angle(self.Z)))
            ax2.set_ylabel('Phase (deg)')
            ax2.grid(True, which='both', linestyle=':')

            ax3.semilogx(freq, self.coherence)
            ax3.set_ylabel('Coherence')
            ax3.set_xlabel('Frequency (Hz)')
            ax3.set_ylim(0, 1.05)
            ax3.grid(True, which='both', linestyle=':')

            plt.tight_layout()
            plot_name = f"{filename}.png"
            plt.savefig(plot_name, dpi=300, bbox_inches='tight')
            plt.close(fig)
            print(f"Saved Plot -> {plot_name}")


if __name__ == "__main__":
    INPUT_DEVICE = 1   # Realtek Line In
    OUTPUT_DEVICE = 6  # Realtek Line Out

    plotter = BodePlotter(
        input_device=INPUT_DEVICE,
        output_device=OUTPUT_DEVICE,
        fs=48000,
        duration_s=30.0,
        output_rms=0.5,
        r_sense=22.060,
        nperseg=32768,
        f_min=20.0,
        f_max=20000.0,
        cal_file="impedance_bode.cal",
        auto_load_calibration=True,
    )

    plotter.ensure_calibration("impedance_bode.cal", prompt_user=True)
    plotter.run()
    plotter.save("impedance_bode_plot")
