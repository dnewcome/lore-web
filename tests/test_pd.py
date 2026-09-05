"""Pd plugin tests: parsing, geometry, and the drawn SVG.

There is no external renderer to check against the way ImageMagick checks
the TIFF plugin, so these assert on the two things that can actually be
wrong: what came out of the file, and where the drawing puts it.  Box
geometry is checked against Pd's own font metrics rather than eyeballed.

    python3 -m unittest discover -s tests -v
"""
import importlib.util
import os
import re
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
_spec = importlib.util.spec_from_file_location(
    "loreweb_plugin_pd", os.path.join(HERE, os.pardir, "plugins", "pd.py"))
pd = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pd)


def patch(body, font=10):
    return f"#N canvas 0 50 450 300 {font};\n" + body


class ParseTest(unittest.TestCase):
    def test_records_end_at_semicolons_not_newlines(self):
        """Pd wraps long boxes across lines; only ';' ends a record."""
        root = pd.parse(patch("#X obj 10 10 select 1 2 3 4\n5 6 7 8;\n"))
        self.assertEqual(len(root.objects), 1)
        self.assertEqual(root.objects[0]["text"], "select 1 2 3 4 5 6 7 8")

    def test_escaped_semicolon_stays_inside_the_message(self):
        """'; pd dsp 1' messages are everywhere and must not split."""
        root = pd.parse(patch("#X msg 34 46 \; pd dsp 1;\n"))
        self.assertEqual(len(root.objects), 1)
        self.assertEqual(root.objects[0]["text"], "; pd dsp 1")

    def test_escaped_dollar_and_comma(self):
        root = pd.parse(patch("#X obj 10 10 metro \\$1;\n"
                              "#X text 10 40 a \\, b;\n"))
        self.assertEqual(root.objects[0]["text"], "metro $1")
        self.assertEqual(root.objects[1]["text"], "a, b")   # comma hugs 'a'

    def test_connections_and_out_of_range_are_kept_separate(self):
        root = pd.parse(patch("#X obj 10 10 t b b;\n#X obj 10 50 print;\n"
                              "#X connect 0 1 1 0;\n#X connect 0 9 7 3;\n"))
        self.assertEqual(root.cords, [(0, 1, 1, 0), (0, 9, 7, 3)])
        svg = pd._draw(root)                     # the bogus cord must not crash
        self.assertIn("<line", svg)

    def test_subpatch_becomes_one_object_in_the_parent(self):
        root = pd.parse(patch(
            "#N canvas 0 50 450 300 guts 0;\n"
            "#X obj 10 10 osc~ 440;\n"
            "#X obj 10 40 dac~;\n"
            "#X connect 0 0 1 0;\n"
            "#X restore 30 30 pd guts;\n"
            "#X obj 30 80 print;\n"))
        self.assertEqual([o["type"] for o in root.objects],
                         ["subpatch", "obj"])
        self.assertEqual(root.objects[0]["text"], "pd guts")
        self.assertEqual(len(root.objects[0]["sub"].objects), 2)

    def test_census_counts_through_subpatches(self):
        root = pd.parse(patch(
            "#N canvas 0 50 450 300 guts 0;\n"
            "#X obj 10 10 osc~ 440;\n#X obj 10 40 gemhead;\n"
            "#X connect 0 0 1 0;\n#X restore 30 30 pd guts;\n"
            "#X text 10 90 a comment;\n"))
        meta = pd._meta(root)
        self.assertEqual(meta["objects"], 3)     # subpatch + its two children
        self.assertEqual(meta["cords"], 1)
        self.assertEqual(meta["subpatches"], 1)
        self.assertEqual(meta["audio"], "yes")   # osc~
        self.assertEqual(meta["externals"], "gemhead")   # not Pd vanilla

    def test_comments_are_not_counted_as_objects(self):
        root = pd.parse(patch("#X text 10 10 hello;\n#X text 10 30 world;\n"))
        self.assertEqual(pd._meta(root)["objects"], 0)

    def test_rejects_a_file_with_no_canvas(self):
        for junk in ("", "not a pd file", "#X obj 10 10 print;\n"):
            with self.subTest(junk=junk[:20]), self.assertRaises(ValueError):
                with tempfile.NamedTemporaryFile("w", suffix=".pd",
                                                 delete=False) as f:
                    f.write(junk)
                try:
                    pd.inspect(f.name, {})
                finally:
                    os.unlink(f.name)

    def test_crlf_patches_parse(self):
        root = pd.parse("#N canvas 0 50 450 300 10;\n"
                        "#X obj 10 10 print;\n".replace("\n", "\r\n")
                        .replace("\r\n", "\n"))
        self.assertEqual(len(root.objects), 1)


class GeometryTest(unittest.TestCase):
    def test_box_width_follows_pd_font_metrics(self):
        """Boxes are sized the way Pd sizes them, not by guesswork."""
        for font, (fw, fh) in pd.FONTS.items():
            with self.subTest(font=font):
                root = pd.parse(patch("#X obj 10 10 print;\n", font=font))
                w, h = pd._size(root.objects[0], font)
                self.assertEqual(w, len("print") * fw + pd.BOX_PAD_X)
                self.assertEqual(h, fh + pd.BOX_PAD_Y)

    def test_message_text_clears_the_pinched_right_edge(self):
        """The invariant, not the constant: text must not reach the flag.

        Asserting the width difference equals FLAG_W would compare the code
        against itself and pass for any value, zero included.
        """
        text = "62 65 69"
        root = pd.parse(patch(f"#X msg 10 40 {text};\n"))
        node = root.objects[0]
        node["w"], node["h"] = pd._size(node, 10)
        flag = min(6, node["w"] / 3)            # matches _shape()
        text_end = node["x"] + 2 + len(text) * pd.FONTS[10][0]
        self.assertLessEqual(text_end, node["x"] + node["w"] - flag)

    def test_port_positions_span_the_box(self):
        root = pd.parse(patch("#X obj 10 10 verylongobjectname;\n"))
        node = root.objects[0]
        node["w"], node["h"] = pd._size(node, 10)
        self.assertAlmostEqual(pd._port_x(node, 0, 3), node["x"] + pd.IO_W / 2)
        self.assertAlmostEqual(pd._port_x(node, 2, 3),
                               node["x"] + node["w"] - pd.IO_W / 2)
        self.assertAlmostEqual(pd._port_x(node, 0, 1), node["x"] + pd.IO_W / 2)

    def test_io_counts_come_from_the_cords(self):
        root = pd.parse(patch("#X obj 10 10 t b b b;\n#X obj 10 60 pack 0 0;\n"
                              "#X connect 0 2 1 1;\n"))
        nin, nout = pd._io_counts(root)
        self.assertEqual(nout[0], 3)     # outlet index 2 implies three
        self.assertEqual(nin[1], 2)
        self.assertEqual(nin[0], 1)      # nothing connects in: the default

    def test_gui_objects_use_their_saved_pixel_size(self):
        root = pd.parse(patch(
            "#X obj 10 10 bng 19 250 50 0 empty empty empty 17 7 0 10 "
            "-262144 -1 -1;\n"
            "#X obj 40 10 cnv 15 300 120 empty empty empty 20 12 0 14 "
            "-233017 -66577 0;\n"
            "#X obj 10 60 hradio 15 1 0 8 empty empty empty 0 -8 0 10 "
            "-262144 -1 -1 0;\n"))
        self.assertEqual(pd._size(root.objects[0], 10), (19, 19))
        self.assertEqual(pd._size(root.objects[1], 10), (300, 120))
        self.assertEqual(pd._size(root.objects[2], 10), (15 * 8, 15))

    def test_iemgui_colour_decoding(self):
        # -262144 is Pd's default light background, six bits per channel
        self.assertEqual(pd._color("-262144"), "#fcfcfc")
        self.assertEqual(pd._color("#ff8800"), "#ff8800")
        self.assertIsNone(pd._color("15"))

    def test_background_colour_survives_a_negative_slider_minimum(self):
        """A negative minimum looks exactly like a colour atom."""
        args = "hsl 128 15 -1 1 0 0 empty empty empty -2 -8 0 10 " \
               "-262144 -1 -1 0 1".split()
        self.assertEqual(pd._bg_color(args), "#fcfcfc")


class DrawTest(unittest.TestCase):
    def svg(self, body, font=10):
        return pd._draw(pd.parse(patch(body, font=font)))

    def test_empty_canvas_draws_nothing(self):
        self.assertIsNone(self.svg(""))

    def test_viewbox_covers_every_object(self):
        svg = self.svg("#X obj -80 -40 print;\n#X obj 300 200 print;\n")
        x0, y0, w, h = (float(v) for v in
                        re.search(r'viewBox="([-\d.]+) ([-\d.]+) '
                                  r'([-\d.]+) ([-\d.]+)"', svg).groups())
        self.assertLessEqual(x0, -80)          # negative coords are common
        self.assertLessEqual(y0, -40)
        self.assertGreaterEqual(x0 + w, 300)
        self.assertGreaterEqual(y0 + h, 200)

    def test_text_is_pinned_to_the_pd_metrics(self):
        """textLength keeps glyphs inside boxes sized with Pd's font table."""
        svg = self.svg("#X obj 10 10 print;\n")
        self.assertIn('textLength="30"', svg)   # 5 chars * 6px at font 10
        self.assertIn('lengthAdjust="spacingAndGlyphs"', svg)

    def test_signal_cords_are_drawn_thicker(self):
        svg = self.svg("#X obj 10 10 osc~ 440;\n#X obj 10 60 dac~;\n"
                       "#X connect 0 0 1 0;\n")
        self.assertIn('stroke-width="2.0"', svg)

    def test_control_outlets_of_tilde_objects_stay_thin(self):
        """snapshot~ is named like a signal object but outputs a float."""
        svg = self.svg("#X obj 10 10 snapshot~;\n#X obj 10 60 print;\n"
                       "#X connect 0 0 1 0;\n")
        self.assertNotIn('stroke-width="2.0"', svg)

    def test_comments_have_no_box(self):
        self.assertNotIn("<path", self.svg("#X text 10 10 just a comment;\n"))

    def test_message_and_object_boxes_have_different_outlines(self):
        obj = self.svg("#X obj 10 10 foo;\n")
        msg = self.svg("#X msg 10 10 foo;\n")
        self.assertNotEqual(re.search(r'<path d="([^"]+)"', obj).group(1),
                            re.search(r'<path d="([^"]+)"', msg).group(1))

    def test_array_data_is_traced(self):
        svg = pd._draw(pd.parse(
            "#N canvas 0 50 450 300 10;\n"
            "#N canvas 0 50 450 300 graph1 0;\n"
            "#X array wave 4 float 0;\n"
            "#A 0 1 -1 1 -1;\n"
            "#X coords 0 1 3 -1 200 140 1;\n"
            "#X restore 30 30 graph;\n"))
        line = re.search(r'<polyline points="([^"]+)"', svg)
        self.assertIsNotNone(line, "array data should be drawn")
        ys = [float(p.split(",")[1]) for p in line.group(1).split()]
        self.assertAlmostEqual(min(ys), 30, delta=1)     # value 1 -> graph top
        self.assertAlmostEqual(max(ys), 170, delta=1)    # value -1 -> bottom

    def test_svg_escapes_markup_in_patch_text(self):
        svg = self.svg("#X text 10 10 a < b & c > d;\n")
        self.assertIn("&lt;", svg)
        self.assertIn("&amp;", svg)
        self.assertNotIn("< b", svg)

    def test_output_is_well_formed_xml(self):
        import xml.etree.ElementTree as ET
        svg = self.svg("#X obj 10 10 osc~ 440;\n#X msg 60 10 \; pd dsp 1;\n"
                       "#X text 10 60 a \\, b < c;\n#X obj 10 90 dac~;\n"
                       "#X connect 0 0 3 0;\n")
        ET.fromstring(svg)      # raises if malformed


if __name__ == "__main__":
    unittest.main()
