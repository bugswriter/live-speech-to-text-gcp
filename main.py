import os
import json
import base64
import argparse
import sys
from dotenv import load_dotenv
from google.oauth2 import service_account
from google.cloud import speech
from google.cloud import storage

# Load environment variables
load_dotenv()

def get_credentials():
    """Decodes the Base64 JSON key and returns a Credentials object."""
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

def upload_to_gcs(bucket_name, source_file_path, credentials):
    """Uploads a file to the bucket and returns the gs:// URI."""
    storage_client = storage.Client(credentials=credentials)
    bucket = storage_client.bucket(bucket_name)
    
    # Use the filename as the blob name
    blob_name = os.path.basename(source_file_path)
    blob = bucket.blob(blob_name)

    print(f"Uploading '{blob_name}' to gs://{bucket_name}...")
    blob.upload_from_filename(source_file_path)
    
    return f"gs://{bucket_name}/{blob_name}", blob

def transcribe_gcs_uri(gcs_uri, credentials):
    """Transcribes audio located at a GCS URI."""
    client = speech.SpeechClient(credentials=credentials)

    audio = speech.RecognitionAudio(uri=gcs_uri)
    
    # Configuration
    config = speech.RecognitionConfig(
        encoding=speech.RecognitionConfig.AudioEncoding.ENCODING_UNSPECIFIED,
        language_code="en-US",
        enable_automatic_punctuation=True,
        model="latest_long" 
    )

    print("Starting transcription job (LongRunningRecognize)...")
    operation = client.long_running_recognize(config=config, audio=audio)

    print("Processing... (this may take time depending on file length)")
    response = operation.result(timeout=1800) # Wait up to 30 mins

    full_transcript = []
    for result in response.results:
        full_transcript.append(result.alternatives[0].transcript)
    
    return " ".join(full_transcript)

# --- MAIN EXECUTION ---
if __name__ == "__main__":
    # 1. Parse Arguments
    parser = argparse.ArgumentParser(description="Transcribe audio using GCP Speech-to-Text.")
    parser.add_argument("file_path", help="Path to the local audio file")
    args = parser.parse_args()

    # 2. Validation
    if not os.path.exists(args.file_path):
        print(f"Error: File '{args.file_path}' not found.")
        sys.exit(1)

    bucket_name = os.getenv("GOOGLE_BUCKET_NAME")
    if not bucket_name:
        print("Error: GOOGLE_BUCKET_NAME is missing from .env")
        sys.exit(1)

    try:
        # 3. Authenticate & Upload
        creds = get_credentials()
        gcs_uri, blob_obj = upload_to_gcs(bucket_name, args.file_path, creds)

        # 4. Transcribe
        text = transcribe_gcs_uri(gcs_uri, creds)

        # 5. Output
        print("\n--- FINAL TRANSCRIPT ---")
        print(text)
        
        # Save to .txt file
        output_file = f"{args.file_path}.txt"
        with open(output_file, "w") as f:
            f.write(text)
        print(f"\nSaved transcript to: {output_file}")

        # Optional: Cleanup Cloud Storage
        # blob_obj.delete()
        # print("Temporary cloud file deleted.")

    except Exception as e:
        print(f"\nAn error occurred: {e}")
