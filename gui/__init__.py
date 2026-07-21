"""GUI package for KayakFit.

Submodules are imported directly (e.g. ``from gui.main_gui import KayakFitGUI``)
rather than eagerly here, so that importing lightweight modules such as
``gui.config_manager`` from the headless workers does not pull in the GUI
toolkit (tkinter / customtkinter).
"""
