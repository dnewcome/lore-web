"""lore-web plugin: Ableton Live sets (.als) -> track-map SVG + session metadata.

.als files are gzipped XML. Extracts track counts/names/types, tempo, and
the devices/plugins used; renders a track-list bar as the preview.
"""
import gzip
import xml.etree.ElementTree as ET
from xml.sax.saxutils import escape

MATCH = [".als"]

TRACK_TYPES = {"AudioTrack": ("#5cb85c", "audio"),
               "MidiTrack": ("#4a9eda", "midi"),
               "ReturnTrack": ("#8a8a8a", "return"),
               "GroupTrack": ("#e0823d", "group")}

# native Ableton devices worth naming (element tag -> display name)
NATIVE = {"Operator": "Operator", "Wavetable": "Wavetable",
          "OriginalSimpler": "Simpler", "MultiSampler": "Sampler",
          "InstrumentImpulse": "Impulse", "DrumGroupDevice": "Drum Rack",
          "InstrumentGroupDevice": "Instrument Rack", "UltraAnalog": "Analog",
          "StringStudio": "Tension", "LoungeLizard": "Electric",
          "Collision": "Collision", "InstrumentMeld": "Meld",
          # common effects
          "Eq8": "EQ Eight", "FilterEQ3": "EQ Three",
          "Compressor2": "Compressor", "GlueCompressor": "Glue Compressor",
          "Reverb": "Reverb", "Delay": "Delay", "PingPongDelay": "Ping Pong",
          "FilterDelay": "Filter Delay", "AutoFilter": "Auto Filter",
          "AutoPan": "Auto Pan", "BeatRepeat": "Beat Repeat",
          "Chorus": "Chorus", "Chorus2": "Chorus-Ensemble",
          "Saturator": "Saturator", "Overdrive": "Overdrive", "Amp": "Amp",
          "CabinetDevice": "Cabinet", "Gate": "Gate", "Limiter": "Limiter",
          "MultibandDynamics": "Multiband Dynamics", "Tuner": "Tuner",
          "PhaserNew": "Phaser-Flanger", "GrainDelay": "Grain Delay",
          "Vocoder": "Vocoder", "Resonator": "Resonators",
          "FrequencyShifter": "Frequency Shifter", "Utility": "Utility"}


def _value(el, tag):
    child = el.find(tag)
    return child.get("Value") if child is not None else None


def _track_name(track):
    for tag in ("Name/EffectiveName", "Name/UserName"):
        el = track.find(tag)
        if el is not None and el.get("Value"):
            return el.get("Value")
    return track.tag


def inspect(path, ctx):
    with gzip.open(path) as f:
        root = ET.parse(f).getroot()
    live = root.find("LiveSet")
    if live is None:
        raise ValueError("no LiveSet element")

    tracks = []   # (type-tag, name)
    counts = {}
    tracks_el = live.find("Tracks")
    if tracks_el is not None:
        for t in tracks_el:
            if t.tag in TRACK_TYPES:
                tracks.append((t.tag, _track_name(t)))
                counts[TRACK_TYPES[t.tag][1]] = \
                    counts.get(TRACK_TYPES[t.tag][1], 0) + 1

    plugins = set()
    for el in live.iter():
        if el.tag in NATIVE:
            plugins.add(NATIVE[el.tag])
        elif el.tag in ("VstPluginInfo", "Vst3PluginInfo", "AuPluginInfo"):
            name = _value(el, "PlugName") or _value(el, "Name")
            if name:
                plugins.add(name)

    tempo = None
    for el in live.iter("Tempo"):
        tempo = _value(el, "Manual")
        if tempo:
            break

    meta = {k: v for k, v in counts.items()}
    if tempo:
        try:
            meta["bpm"] = round(float(tempo))
        except ValueError:
            pass
    if plugins:
        meta["plugins"] = sorted(plugins)[:12]

    # preview: one colored row per track with its name
    rows, rh, w = [], 18, 460
    shown = tracks[:14]
    h = max(len(shown) * rh, rh)
    rows.append(f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" '
                f'height="{h}" viewBox="0 0 {w} {h}" '
                f'font-family="system-ui,sans-serif" font-size="11">')
    for i, (ttype, name) in enumerate(shown):
        color = TRACK_TYPES[ttype][0]
        y = i * rh
        rows.append(f'<rect x="0" y="{y + 2}" width="{w}" height="{rh - 4}" '
                    f'rx="3" fill="{color}" fill-opacity="0.25"/>')
        rows.append(f'<rect x="0" y="{y + 2}" width="4" height="{rh - 4}" '
                    f'fill="{color}"/>')
        rows.append(f'<text x="10" y="{y + rh - 6}" fill="currentColor">'
                    f'{escape(name[:60])}</text>')
    if len(tracks) > len(shown):
        rows[-1] = rows[-1].replace("</text>",
                                    f" (+{len(tracks) - len(shown)} more)</text>")
    rows.append("</svg>")

    return {"meta": meta, "kind": "ableton", "preview":
            ("".join(rows).encode(), "svg")}
