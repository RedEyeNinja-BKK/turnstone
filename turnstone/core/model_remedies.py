"""What a chat user is told to do about a model that isn't available (#1359).

Chat users can't run ``/model``: the channels send every message to the send endpoint, which runs
no slash commands. Two chat messages therefore point at whoever can fix the model, in the same
words: the channels' reply when a conversation can't start on its model, and the refusal of a
conversation held on it. Both read the words from here, because the engine never imports the
channels. Each ends the sentence its own way: nothing was recorded when a conversation can't start,
so that reply says to try again, while a held conversation has kept the message.
"""

from __future__ import annotations

#: The remedy a chat user can act on, without its ending: an admin fixes the model.
CHAT_MODEL_REMEDY = "Ask a Turnstone admin to check it in the Models tab"
