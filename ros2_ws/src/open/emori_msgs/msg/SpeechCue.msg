# One sentence of ASSISTANT speech, published at playback onset under
# /humans/persons/<id>/speech_cue.
#
# Deliberately NOT hri_msgs/LiveSpeech: LiveSpeech carries what the HUMAN said.
# This is what the robot's voice is doing right now, which is what the engagement
# node needs in order to move the head while that sentence is spoken.
#
# `text` is the CUE-STRIPPED sentence actually synthesised; it MUST never contain
# a '[' - the bracket vocabulary is creative direction for the LLM and is removed
# upstream, before this wire.
std_msgs/Header header

string person_id
string session_id
uint32 seq

string text                          # cue-stripped spoken sentence; never contains '['
# The family the robot PERFORMS for this sentence: the authored cue family when
# one was forwarded upstream, otherwise the family the hostapp read from the
# sentence itself (or carried from an earlier sentence of the turn). The hostapp
# stamps it on the relayed envelope BEFORE the relay fans out, together with the
# wire-only fields cue_family_source ("pokamind"|"text"|"carried"|"dialogue"|"";
# "pokamind" means upstream-authored) and cue_slug, so an owner=ros engagement node
# performs the same family from the same message and the graph describes the motion
# that actually happened.
string cue_category                  # one of the 6 families, "" when none
string cue_body                      # freeform direction; reserved, currently always empty
string lang
string source                        # "turn" | "scenario"

float32 est_duration                 # seconds, 0.333 * word_count clamped [0.9, 6.0]

builtin_interfaces/Time source_time  # the envelope's own timestamp (correlation)

bool ended                           # true for the TTS-ended marker, pokamind_tts_ended (text empty)

# What Pokamind itself forwarded for this sentence, "" when it forwarded none. It can differ
# from `cue_category`, which is the family the robot performs: a direction can name another
# family, and a sentence with no cue can inherit the turn's.
string cue_category_authored
