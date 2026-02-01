import os
import json
import base64
import argparse
import sys
import subprocess
from dotenv import load_dotenv
from google.oauth2 import service_account
from google.cloud import speech
from google.cloud import storage

# Load environment variables
load_dotenv()

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

def convert_to_optimized_wav(input_path):
    """
    Converts audio to GCP's 'Golden Format': Mono, 16kHz, Linear16 WAV.
    This prevents MP3 decoding errors and improves accuracy.
    """
    output_path = input_path + ".optimized.wav"
    print(f"Converting '{input_path}' to optimized WAV...")
    
    command = [
        "ffmpeg", "-i", input_path, 
        "-ac", "1",      # Mix to Mono
        "-ar", "16000",  # Resample to 16kHz
        "-y",            # Overwrite
        output_path
    ]
    
    try:
        subprocess.run(command, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return output_path
    except subprocess.CalledProcessError:
        print("Error: FFmpeg failed.")
        sys.exit(1)
    except FileNotFoundError:
        print("Error: 'ffmpeg' not found. Please install it (sudo apt install ffmpeg).")
        sys.exit(1)

def upload_to_gcs(bucket_name, source_file_path, credentials):
    storage_client = storage.Client(credentials=credentials)
    bucket = storage_client.bucket(bucket_name)
    blob_name = os.path.basename(source_file_path)
    blob = bucket.blob(blob_name)

    print(f"Uploading to gs://{bucket_name}/{blob_name}...")
    blob.upload_from_filename(source_file_path)
    return f"gs://{bucket_name}/{blob_name}", blob

def transcribe_gcs_uri(gcs_uri, credentials):
    client = speech.SpeechClient(credentials=credentials)
    audio = speech.RecognitionAudio(uri=gcs_uri)
    
    # Speaker Diarization Config
    diarization_config = speech.SpeakerDiarizationConfig(
        enable_speaker_diarization=True,
        min_speaker_count=1,
        max_speaker_count=6 # Adjust if you know the exact number
    )

    # Recognition Config (Strictly Linear16 for best results)
    config = speech.RecognitionConfig(
        encoding=speech.RecognitionConfig.AudioEncoding.LINEAR16,
        sample_rate_hertz=16000,
        language_code="en-US",
        enable_automatic_punctuation=True,
        audio_channel_count=1, 
        model="latest_long", # Best model for long-form content
        diarization_config=diarization_config
    )

    print("Starting transcription job (LongRunning)...")
    operation = client.long_running_recognize(config=config, audio=audio)

    print("Processing... (This takes about 30% of the audio duration)")
    response = operation.result(timeout=2700) 

    # Processing Results
    if not response.results:
        return "No speech detected."

    result = response.results[-1]
    if not result.alternatives[0].words:
        return result.alternatives[0].transcript

    # Reconstruct transcript with Speaker Tags
    full_transcript = []
    current_speaker = None
    current_sentence = []
    
    words_info = result.alternatives[0].words
    for word_info in words_info:
        speaker_tag = word_info.speaker_tag
        
        if current_speaker is None:
            current_speaker = speaker_tag
        
        if speaker_tag != current_speaker:
            full_transcript.append(f"[Speaker {current_speaker}]: {' '.join(current_sentence)}")
            current_sentence = []
            current_speaker = speaker_tag
            
        current_sentence.append(word_info.word)

    if current_sentence:
        full_transcript.append(f"[Speaker {current_speaker}]: {' '.join(current_sentence)}")

    return "\n".join(full_transcript)

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("file_path", help="Path to local audio file")
    args = parser.parse_args()

    if not os.path.exists(args.file_path):
        print(f"Error: File '{args.file_path}' not found.")
        sys.exit(1)

    bucket_name = os.getenv("GOOGLE_BUCKET_NAME")
    if not bucket_name:
        print("Error: GOOGLE_BUCKET_NAME missing in .env")
        sys.exit(1)

    temp_wav_path = None
    try:
        creds = get_credentials()
        
        # 1. Convert
        temp_wav_path = convert_to_optimized_wav(args.file_path)

        # 2. Upload
        gcs_uri, blob_obj = upload_to_gcs(bucket_name, temp_wav_path, creds)
        
        # 3. Transcribe
        text = transcribe_gcs_uri(gcs_uri, creds)

        print("\n--- FINAL TRANSCRIPT ---")
        print(text)
        
        output_file = f"{args.file_path}.txt"
        with open(output_file, "w") as f:
            f.write(text)
        print(f"\nSaved to: {output_file}")

        # 4. Cleanup Cloud
        print("Cleaning up cloud storage...")
        blob_obj.delete()

    except Exception as e:
        print(f"\nCRITICAL ERROR: {e}")
    
    finally:
        # 5. Cleanup Local Temp
        if temp_wav_path and os.path.exists(temp_wav_path):
            os.remove(temp_wav_path)
            print("Temporary WAV deleted.")
