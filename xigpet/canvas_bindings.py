"""Release Tcl callback commands when their canvas items are replaced."""
import tkinter as tk


def bind_item(canvas, tag, sequence, callback):
    command = canvas.tag_bind(tag, sequence, callback)
    if not hasattr(canvas, "_pet_item_bindings"):
        canvas._pet_item_bindings = []
    canvas._pet_item_bindings.append((tag, sequence, command))
    return command


def clear_items(canvas):
    for tag, sequence, command in getattr(canvas, "_pet_item_bindings", ()):
        try:
            canvas.tag_unbind(tag, sequence, command)
        except tk.TclError:
            pass
    canvas._pet_item_bindings = []
    canvas.delete("all")
