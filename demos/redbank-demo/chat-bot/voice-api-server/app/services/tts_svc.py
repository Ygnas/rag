# Copyright 2025 IBM, Red Hat
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import io
import wave
import struct
import math
import openai
import httpx


class TTSService:
    def __init__(self, url, route=None, voice=None):
        self.url = url
        # Use the same speaker as the working tts.py implementation
        self.speaker = voice if voice else "af_sky+af_bella"

    def synthesize(self, text):
        """Generate speech audio from text using the TTS service"""
        try:
            unverified_client = httpx.Client(verify=False)

            client = openai.OpenAI(
                base_url=self.url,
                api_key="not-needed",
                http_client=unverified_client,
            )

            with client.audio.speech.with_streaming_response.create(
                model="kokoro", voice=self.speaker, input=text
            ) as response:
                # Read the audio data into bytes
                audio_data = b"".join(response.iter_bytes())
                return audio_data
        except Exception as e:
            # Fallback to simple sine wave if TTS service fails
            print(f"TTS service error: {e}, falling back to placeholder audio")
            return self._generate_placeholder_audio(text)

    def _generate_placeholder_audio(self, text):
        """Generate a simple sine wave audio as a fallback"""
        # Generate a 2-second sine wave at 440 Hz as a placeholder
        sample_rate = 22050
        duration = 2.0
        frequency = 440.0

        frames = []
        for i in range(int(sample_rate * duration)):
            value = int(
                32767 * 0.3 * math.sin(2 * math.pi * frequency * i / sample_rate)
            )
            frames.append(struct.pack("<h", value))

        # Create WAV file in memory
        wav_buffer = io.BytesIO()
        with wave.open(wav_buffer, "wb") as wav_file:
            wav_file.setnchannels(1)  # Mono
            wav_file.setsampwidth(2)  # 16-bit
            wav_file.setframerate(sample_rate)
            wav_file.writeframes(b"".join(frames))

        wav_buffer.seek(0)
        return wav_buffer.getvalue()
