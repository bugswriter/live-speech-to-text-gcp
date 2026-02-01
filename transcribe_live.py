import os
import sys
import queue
import json
import base64
import pyaudio
from dotenv import load_dotenv
from google.oauth2 import service_account
from google.cloud import speech

load_dotenv()

# Audio recording parameters (Must be 16000Hz for best accuracy)
RATE = 16000
CHUNK = int(RATE / 10)  # 100ms

def get_credentials():
    b64_key = os.getenv("GOOGLE_CREDENTIALS_BASE64")
    if not b64_key:
        print("Error: GOOGLE_CREDENTIALS_BASE64 is missing from .env")
        sys.exit(1)
    
    try:
        json_key = base64.b64decode(b64_key).decode("utf-8")
        key_dict = json.loads(json_key)
        return service_account.Credentials.from_service_account_info(key_dict)
    except Exception as e:
        print(f"Error decoding credentials: {e}")
        sys.exit(1)

class MicrophoneStream:
    """Opens a recording stream as a generator yielding the audio chunks."""
    def __init__(self, rate, chunk):
        self._rate = rate
        self._chunk = chunk
        self._buff = queue.Queue()
        self.closed = True

    def __enter__(self):
        self._audio_interface = pyaudio.PyAudio()
        self._audio_stream = self._audio_interface.open(
            format=pyaudio.paInt16,
            channels=1, 
            rate=self._rate,
            input=True,
            frames_per_buffer=self._chunk,
            stream_callback=self._fill_buffer,
        )
        self.closed = False
        return self

    def __exit__(self, type, value, traceback):
        self._audio_stream.stop_stream()
        self._audio_stream.close()
        self.closed = True
        self._buff.put(None)
        self._audio_interface.terminate()

    def _fill_buffer(self, in_data, frame_count, time_info, status_flags):
        self._buff.put(in_data)
        return None, pyaudio.paContinue

    def generator(self):
        while not self.closed:
            chunk = self._buff.get()
            if chunk is None: return
            data = [chunk]
            while True:
                try:
                    chunk = self._buff.get(block=False)
                    if chunk is None: return
                    data.append(chunk)
                except queue.Empty:
                    break
            yield b"".join(data)

def listen_print_loop(responses):
    print("\nListening... (Press Ctrl+C to stop)")
    for response in responses:
        if not response.results: continue
        result = response.results[0]
        if not result.alternatives: continue

        transcript = result.alternatives[0].transcript

        # "is_final=False" means the user is still speaking
        if not result.is_final:
            sys.stdout.write(f"\r\033[K> {transcript}")
            sys.stdout.flush()
        else:
            # "is_final=True" means the sentence is done. Check Speaker Tag.
            # Live speaker tags are attached to the last word.
            speaker_tag = "?"
            if result.alternatives[0].words:
                speaker_tag = result.alternatives[0].words[-1].speaker_tag
            
            print(f"\r\033[K[Speaker {speaker_tag}]: {transcript}")

def main():
    creds = get_credentials()
    client = speech.SpeechClient(credentials=creds)

    diarization_config = speech.SpeakerDiarizationConfig(
        enable_speaker_diarization=True,
        min_speaker_count=1,
        max_speaker_count=4
    )

    config = speech.RecognitionConfig(
        encoding=speech.RecognitionConfig.AudioEncoding.LINEAR16,
        sample_rate_hertz=RATE,
        language_code="en-US",
        enable_automatic_punctuation=True,
        diarization_config=diarization_config
    )

    streaming_config = speech.StreamingRecognitionConfig(
        config=config,
        interim_results=True 
    )

    with MicrophoneStream(RATE, CHUNK) as stream:
        audio_generator = stream.generator()
        requests = (speech.StreamingRecognizeRequest(audio_content=content) for content in audio_generator)

        try:
            responses = client.streaming_recognize(config=streaming_config, requests=requests)
            listen_print_loop(responses)
        except Exception as e:
            if "Exceeded" in str(e):
                print("\n\n[INFO] Google limits live streams to ~5 minutes.")
            else:
                print(f"\nError: {e}")

if __name__ == "__main__":
    main()
