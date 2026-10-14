"""A generic yes/no modal. `app.push_screen_wait(ConfirmScreen("..."))` returns
the chosen bool once dismissed."""

from __future__ import annotations

from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Static


class ConfirmScreen(ModalScreen[bool]):
    DEFAULT_CSS = """
    ConfirmScreen {
        align: center middle;
    }
    #confirm-box {
        width: 60;
        height: auto;
        border: thick $error;
        background: $surface;
        padding: 1 2;
    }
    #confirm-buttons {
        height: auto;
        margin-top: 1;
        align: right middle;
    }
    """

    def __init__(self, question: str) -> None:
        super().__init__()
        self.question = question

    def compose(self):
        with Vertical(id="confirm-box"):
            yield Static(self.question)
            with Horizontal(id="confirm-buttons"):
                yield Button("Cancel", id="cancel")
                yield Button("Confirm", id="confirm", variant="error")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "confirm")
