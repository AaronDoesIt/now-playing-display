# Bundled model

`yamnet.tflite` is Google's YAMNet audio event classifier (521 AudioSet classes,
MobileNet v1, Apache-2.0 licence), in the TensorFlow Lite packaging published
for MediaPipe's audio classifier.

| | |
|---|---|
| Source | https://storage.googleapis.com/mediapipe-models/audio_classifier/yamnet/float32/1/yamnet.tflite |
| Downloaded | 2026-11-10 |
| Size | 4,126,810 bytes |
| SHA-256 | `4d8b4a53282dc83ef04e3e7dbc4fbc98082e34e44ed798e16c3a0cdd4c584faf` |
| Input | 15,600 float samples: 0.975 s of 16 kHz mono audio in the range -1..1 |
| Output | 521 scores (0..1). The daemon reads class 132 "Music" and class 0 "Speech" |

The class names are stored inside the file (it is also a zip archive holding
`yamnet_label_list.txt`), and the daemon looks the two indices up by name.

It is used only by `nowplaying/musicdetect.py`, to decide locally whether the
room is hearing music before any recognition request is made.
