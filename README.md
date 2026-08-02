# OpenImpedanceLab

OpenImpedanceLab is an open-source audio impedance measurement toolkit for turning commodity sound hardware into a high-performance impedance analyzer.

```text
Soundcard line out ──+── series resistor (e.g. 22 Ω) ──+── DUT / driver ── GND
                     │                                 │
                     │                                 └── soundcard line in (channel 2)
                     └────────────────────────────────────── soundcard line in (channel 1)
```


## Features:

* 3-point calibration workflow: channel mismatch, open, and short
* Calibration stored in a `.cal` file
* Coherence-weighted transfer function estimation
* Complex impedance extraction from voltage-divider measurements
* Automatic export of `.csv` files with magnitude, phase, and coherence
* Measurement plotting and visualization


## Example Measurement:

Example impedance measurement of a loudspeaker from 20 to 20K Hz with coherence plot

![Example impedance Bode plot](example/impedance_bode_plot.png)


## License:
Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0).
