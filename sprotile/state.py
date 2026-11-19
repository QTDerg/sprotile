"""Runtime values that several modules read and write: the picker operator and its draw handler, the
rectangle-corner overlay, the image shader, preset preview icons and the old-copy notice. They live
here, read as state.<name>, so every module sees the same value.
"""


# ---------------------------------------------------------------------------
# Global state
#
# The draw handles and the live modal operators are mirrored into module level
# globals so they can always be shut down, even if the operator instance that
# created them is gone.  Blender kills modal operators without warning on file
# load, area close, script reload, etc.  A leaked draw handler is what makes
# the overlay "stick" on screen forever, and it hard-crashes Blender once the
# addon is unregistered underneath it.
# ---------------------------------------------------------------------------

_preview_operator = None
_preview_draw_handle = None
# Set at register when an older copy is still loaded beside this one.
_legacy_notice = None
# The last message a live Edit Blend control refused with. Blender swallows exceptions raised in property setters
# (the slider just snaps back), so the Texture Blend panel shows this instead. Cleared by the next change that works.
last_control_refusal = None
_preset_previews = None
_corner_draw_handle = None


_image_shader = None
