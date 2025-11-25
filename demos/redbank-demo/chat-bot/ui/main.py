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

from flask import Flask, render_template, request, jsonify, send_file
from flask_cors import CORS
import requests
import base64
import os

app = Flask(__name__)
CORS(app)

# Voice Processing API Configuration
VOICE_API_BASE_URL = os.getenv("VOICE_API_BASE_URL", "http://localhost:8000")
VOICE_API_ENDPOINTS = {
    "transcribe": f"{VOICE_API_BASE_URL}/api/voice/transcribe",
    "complete": f"{VOICE_API_BASE_URL}/api/voice/complete",
    "chat": f"{VOICE_API_BASE_URL}/api/voice/chat",
    "session_start": f"{VOICE_API_BASE_URL}/api/voice/session/start",
    "conversation_clear": f"{VOICE_API_BASE_URL}/api/voice/conversation/clear",
    "speak": f"{VOICE_API_BASE_URL}/api/voice/speak",
}

# Mock response configuration
MOCK_RESPONSE_ENABLED = os.getenv("MOCK_RESPONSE_ENABLED", "False").lower() == "true"
DEFAULT_MOCK_TEXT = "This is a mock AI response"


def text_to_speech(text):
    """Generate speech audio from text using the voice-api-server TTS service"""
    try:
        # Call the voice-api-server speak endpoint
        response = requests.post(VOICE_API_ENDPOINTS["speak"], params={"text": text})

        if response.status_code == 200:
            # Save to output.wav for compatibility
            audio_data = response.content
            if os.path.exists("output.wav"):
                os.remove("output.wav")
            with open("output.wav", "wb") as f:
                f.write(audio_data)
            return audio_data
        else:
            print(
                f"TTS Error: Voice API returned status {response.status_code}: {response.text}"
            )
            return None
    except Exception as e:
        print(f"TTS Error: {e}")
        return None


@app.route("/")
def index():
    return render_template(
        "index.html",
        mock_enabled=MOCK_RESPONSE_ENABLED,
        default_mock_text=DEFAULT_MOCK_TEXT,
    )


@app.route("/mock_response", methods=["POST"])
def mock_response():
    try:
        mock_text = request.form.get("mock_text", DEFAULT_MOCK_TEXT)

        return jsonify(
            {
                "success": True,
                "type": "text",
                "data": mock_text,
                "message": "Mock response generated",
                "mock": True,
            }
        )
    except Exception as e:
        return jsonify({"error": f"Error generating mock response: {str(e)}"}), 500


@app.route("/transcribe", methods=["POST"])
def transcribe_audio():
    """Mock endpoint that simulates audio transcription - returns text"""
    try:
        if "audio" not in request.files:
            return jsonify({"error": "No audio file provided"}), 400

        audio_file = request.files["audio"]
        if audio_file.filename == "":
            return jsonify({"error": "No audio file selected"}), 400

        # Mock transcription response
        mock_transcription = "Hello! This is a mock transcription of your audio. In a real implementation, this would be the actual transcribed text from your audio processing API."

        return mock_transcription, 200, {"Content-Type": "text/plain"}

    except Exception as e:
        return jsonify({"error": f"Error processing audio: {str(e)}"}), 500


@app.route("/text-to-speech", methods=["POST"])
def text_to_speech_endpoint():
    """Text-to-speech endpoint using voice-api-server"""
    try:
        text = request.form.get("text")
        if not text:
            return jsonify({"error": "No text provided"}), 400

        # Call the voice-api-server speak endpoint
        response = requests.post(VOICE_API_ENDPOINTS["speak"], params={"text": text})

        if response.status_code == 200:
            # Save to output.wav for compatibility
            audio_data = response.content
            with open("output.wav", "wb") as f:
                f.write(audio_data)
            return send_file("output.wav", mimetype="audio/wav")
        else:
            return jsonify(
                {
                    "error": f"TTS API request failed with status code: {response.status_code}",
                    "details": response.text,
                }
            ), 400
    except Exception as e:
        return jsonify({"error": f"Error generating speech: {str(e)}"}), 500


@app.route("/voice/complete", methods=["POST"])
def voice_complete():
    """Complete voice-to-voice pipeline using the FastAPI voice processing service"""
    try:
        if "audio" not in request.files:
            return jsonify({"error": "No audio file provided"}), 400

        audio_file = request.files["audio"]
        if audio_file.filename == "":
            return jsonify({"error": "No audio file selected"}), 400

        # Read the audio file data
        audio_data = audio_file.read()

        # Send to voice processing API complete endpoint
        files = {"file": (audio_file.filename, audio_data, audio_file.content_type)}
        response = requests.post(VOICE_API_ENDPOINTS["complete"], files=files)

        if response.status_code == 200:
            response_data = response.json()

            # Generate TTS audio from agent response
            agent_text = response_data.get("agent_text", "")
            tts_audio = text_to_speech(agent_text)

            # Convert TTS audio to base64
            tts_base64 = (
                base64.b64encode(tts_audio).decode("ascii") if tts_audio else ""
            )

            return jsonify(
                {
                    "success": True,
                    "transcript": response_data.get("transcript", ""),
                    "agent_text": agent_text,
                    "audio": tts_base64,
                    "type": "complete",
                    "message": "Voice-to-voice processing completed with TTS",
                }
            )
        else:
            return jsonify(
                {
                    "error": f"Voice API request failed with status code: {response.status_code}",
                    "details": response.text,
                }
            ), 400

    except requests.exceptions.RequestException as e:
        return jsonify({"error": f"Network error: {str(e)}"}), 500
    except Exception as e:
        return jsonify({"error": f"Error: {str(e)}"}), 500


@app.route("/output.wav")
def get_output_audio():
    """Serve the generated output.wav file"""
    try:
        if os.path.exists("output.wav"):
            return send_file("output.wav", as_attachment=True)
        else:
            return jsonify({"error": "output.wav not found"}), 404
    except Exception as e:
        return jsonify({"error": f"Error serving audio: {str(e)}"}), 500


@app.route("/voice/chat", methods=["POST"])
def voice_chat():
    """Text-based chat with the voice processing agent"""
    try:
        text = request.form.get("text")
        if not text:
            return jsonify({"error": "No text provided"}), 400

        # Send to voice processing API chat endpoint
        response = requests.post(f"{VOICE_API_ENDPOINTS['chat']}?text={text}")

        if response.status_code == 200:
            response_data = response.json()

            # Generate TTS audio from agent response
            agent_response = response_data.get("agent_response", "")
            tts_audio = text_to_speech(agent_response)

            # Convert TTS audio to base64
            tts_base64 = (
                base64.b64encode(tts_audio).decode("ascii") if tts_audio else ""
            )

            return jsonify(
                {
                    "success": True,
                    "user_input": response_data.get("user_input", ""),
                    "agent_response": agent_response,
                    "conversation_length": response_data.get("conversation_length", 0),
                    "audio": tts_base64,
                    "type": "chat",
                    "message": "Chat completed with TTS",
                }
            )
        else:
            return jsonify(
                {
                    "error": f"Chat API request failed with status code: {response.status_code}",
                    "details": response.text,
                }
            ), 400

    except requests.exceptions.RequestException as e:
        return jsonify({"error": f"Network error: {str(e)}"}), 500
    except Exception as e:
        return jsonify({"error": f"Error: {str(e)}"}), 500


@app.route("/voice/session/start", methods=["POST"])
def voice_session_start():
    """Start a new agent session"""
    try:
        response = requests.post(VOICE_API_ENDPOINTS["session_start"])

        if response.status_code == 200:
            response_data = response.json()
            return jsonify(
                {
                    "success": True,
                    "session_id": response_data.get("session_id"),
                    "status": response_data.get("status"),
                    "message": "Session started successfully",
                }
            )
        else:
            return jsonify(
                {
                    "error": f"Session start failed with status code: {response.status_code}",
                    "details": response.text,
                }
            ), 400

    except requests.exceptions.RequestException as e:
        return jsonify({"error": f"Network error: {str(e)}"}), 500
    except Exception as e:
        return jsonify({"error": f"Error: {str(e)}"}), 500


@app.route("/voice/conversation/clear", methods=["POST"])
def voice_conversation_clear():
    """Clear the conversation history"""
    try:
        response = requests.post(VOICE_API_ENDPOINTS["conversation_clear"])

        if response.status_code == 200:
            response_data = response.json()
            return jsonify(
                {
                    "success": True,
                    "status": response_data.get("status"),
                    "message": response_data.get("message", "Conversation cleared"),
                }
            )
        else:
            return jsonify(
                {
                    "error": f"Clear conversation failed with status code: {response.status_code}",
                    "details": response.text,
                }
            ), 400

    except requests.exceptions.RequestException as e:
        return jsonify({"error": f"Network error: {str(e)}"}), 500
    except Exception as e:
        return jsonify({"error": f"Error: {str(e)}"}), 500


@app.route("/upload_audio", methods=["POST"])
def upload_audio():
    try:
        if "audio" not in request.files:
            return jsonify({"error": "No audio file provided"}), 400

        audio_file = request.files["audio"]
        api_endpoint = request.form.get("api_endpoint")

        if not api_endpoint:
            return jsonify({"error": "No API endpoint provided"}), 400

        if audio_file.filename == "":
            return jsonify({"error": "No audio file selected"}), 400

        # Send to API endpoint
        files = {
            "audio": (audio_file.filename, audio_file.stream, audio_file.content_type)
        }
        response = requests.post(api_endpoint, files=files)

        if response.status_code == 200:
            content_type = response.headers.get("content-type", "")

            if "audio" in content_type:
                # Direct audio response
                audio_base64 = base64.b64encode(response.content).decode("utf-8")
                return jsonify(
                    {"success": True, "audio": audio_base64, "type": "audio"}
                )
            elif "application/json" in content_type:
                # JSON response
                try:
                    response_data = response.json()
                    if "audio" in response_data:
                        return jsonify(
                            {
                                "success": True,
                                "audio": response_data["audio"],
                                "type": "audio",
                                "message": response_data.get("message", ""),
                            }
                        )
                    else:
                        return jsonify(
                            {"success": True, "data": response_data, "type": "json"}
                        )
                except Exception:
                    return jsonify(
                        {"success": True, "data": response.text, "type": "text"}
                    )
            else:
                # Text response
                return jsonify({"success": True, "data": response.text, "type": "text"})
        else:
            return jsonify(
                {
                    "error": f"API request failed with status code: {response.status_code}",
                    "details": response.text,
                }
            ), 400

    except requests.exceptions.RequestException as e:
        return jsonify({"error": f"Network error: {str(e)}"}), 500
    except Exception as e:
        return jsonify({"error": f"Error: {str(e)}"}), 500


if __name__ == "__main__":
    print("Starting Flask web app...")
    print("Go to: http://localhost:3000")
    app.run(debug=True, host="0.0.0.0", port=3000)
