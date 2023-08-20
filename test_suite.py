# import pyaudio
import argparse
import asyncio
import aiohttp
import json
import os
import sys
import wave
import websockets
import streamlit as st
import time
import threading
from threading import Thread
from datetime import datetime
import streamlit_scrollable_textbox as stx
from streamlit.components.v1 import html
try:
    # Streamlit >= 1.12.0
    from streamlit.runtime.scriptrunner import add_script_run_ctx
    from streamlit.runtime.scriptrunner.script_run_context import get_script_run_ctx
except:
    # Streamlit <= 1.11.0
    from streamlit.scriptrunner import add_script_run_ctx
    from streamlit.scriptrunner.script_run_context import get_script_run_ctx


startTime = datetime.now()

all_mic_data = []
all_transcripts = ['starting...']
__STATUS__ = False
# FORMAT = pyaudio.paInt16
CHANNELS = 1
RATE = 16000
CHUNK = 8000

st.title('Speech Recognition')

# Mimic sending a real-time stream by sending this many seconds of audio at a time.
# Used for file "streaming" only.
REALTIME_RESOLUTION = 0.250

subtitle_line_counter = 0


def subtitle_time_formatter(seconds, separator):
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    secs = int(seconds % 60)
    millis = int((seconds - int(seconds)) * 1000)
    return f"{hours:02}:{minutes:02}:{secs:02}{separator}{millis:03}"


def subtitle_formatter(response, format):
    global subtitle_line_counter
    subtitle_line_counter += 1

    start = response["start"]
    end = start + response["duration"]
    transcript = response.get("channel", {}).get("alternatives", [{}])[0].get("transcript", "")

    separator = "," if format == "srt" else '.'
    prefix = "- " if format == "vtt" else ""
    subtitle_string = (
        f"{subtitle_line_counter}\n"
        f"{subtitle_time_formatter(start, separator)} --> "
        f"{subtitle_time_formatter(end, separator)}\n"
        f"{prefix}{transcript}\n\n"
    )

    return subtitle_string


# Used for microphone streaming only.
# def mic_callback(input_data, frame_count, time_info, status_flag):
#     audio_queue.put_nowait(input_data)
#     return (input_data, pyaudio.paContinue)


async def run(key, method, format, **kwargs):
    audio_queue = asyncio.Queue()
    deepgram_url = f'{kwargs["host"]}/v1/listen?punctuate=true'
    
    if kwargs["model"]:
        deepgram_url += f"&model={kwargs['model']}"

    if kwargs["tier"]:
        deepgram_url += f"&tier={kwargs['tier']}"

    if method == "mic":
        deepgram_url += "&encoding=linear16&sample_rate=16000"

    elif method == "wav":
        data = kwargs["data"]
        deepgram_url += f'&channels={kwargs["channels"]}&sample_rate={kwargs["sample_rate"]}&encoding=linear16'

    # Connect to the real-time streaming endpoint, attaching our credentials.
    async with websockets.connect(
        deepgram_url, extra_headers={"Authorization": "Token {}".format(key)}
    ) as ws:
        print(f'ℹ️  Request ID: {ws.response_headers.get("dg-request-id")}')
        if kwargs["model"]:
            print(f'ℹ️  Model: {kwargs["model"]}')
        if kwargs["tier"]:
            print(f'ℹ️  Tier: {kwargs["tier"]}')
        print("🟢 (1/5) Successfully opened Deepgram streaming connection")

        async def sender(ws):
            global __STATUS__
            print(
                f'🟢 (2/5) Ready to stream {method if (method == "mic" or method == "url") else kwargs["filepath"]} audio to Deepgram{". Speak into your microphone to transcribe." if method == "mic" else ""}'
            )

            if method == "mic":
                try:
                    while True:
                        if threading.current_thread().stopped(): 
                            print("----------------- Called stopped -----------------")
                            await ws.send(json.dumps({"type": "CloseStream"}))
                            print(
                                "🟢 (5/5) Successfully closed Deepgram connection, waiting for final transcripts if necessary"
                            )
                            await ws.close()
                            raise Exception("Ending...")
                        mic_data = await audio_queue.get()
                        all_mic_data.append(mic_data)
                        await ws.send(mic_data)
                except websockets.exceptions.ConnectionClosedOK:
                    await ws.send(json.dumps({"type": "CloseStream"}))
                    print(
                        "🟢 (5/5) Successfully closed Deepgram connection, waiting for final transcripts if necessary"
                    )
                    __STATUS__ = True
                except Exception as e:
                    print(f"Error while sending: {str(e)}")
                    raise

            elif method == "url":
                # Listen for the connection to open and send streaming audio from the URL to Deepgram
                async with aiohttp.ClientSession() as session:
                    async with session.get(kwargs["url"]) as audio:
                        while True:
                            if threading.current_thread().stopped(): 
                                print("----------------- Called stopped -----------------")
                                await ws.send(json.dumps({"type": "CloseStream"}))
                                print(
                                    "🟢 (5/5) Successfully closed Deepgram connection, waiting for final transcripts if necessary"
                                )
                                await ws.close()
                                raise Exception("Ending...")
                            remote_url_data = await audio.content.readany()
                            await ws.send(remote_url_data)

                            # If no data is being sent from the live stream, then break out of the loop.
                            if not remote_url_data:
                                break

            elif method == "wav":
                nonlocal data
                # How many bytes are contained in one second of audio?
                byte_rate = (
                    kwargs["sample_width"] * kwargs["sample_rate"] * kwargs["channels"]
                )
                # How many bytes are in `REALTIME_RESOLUTION` seconds of audio?
                chunk_size = int(byte_rate * REALTIME_RESOLUTION)

                try:
                    while len(data):
                        if threading.current_thread().stopped(): 
                            print("----------------- Called stopped -----------------")
                            await ws.send(json.dumps({"type": "CloseStream"}))
                            print(
                                "🟢 (5/5) Successfully closed Deepgram connection, waiting for final transcripts if necessary"
                            )
                            await ws.close()
                            raise Exception("Ending...")
                        chunk, data = data[:chunk_size], data[chunk_size:]
                        # Mimic real-time by waiting `REALTIME_RESOLUTION` seconds
                        # before the next packet.
                        await asyncio.sleep(REALTIME_RESOLUTION)
                        # Send the data
                        await ws.send(chunk)

                    await ws.send(json.dumps({"type": "CloseStream"}))
                    print(
                        "🟢 (5/5) Successfully closed Deepgram connection, waiting for final transcripts if necessary"
                    )
                    __STATUS__ = True
                except Exception as e:
                    print(f"🔴 ERROR: Something happened while sending, {e}")
                    raise e

            return
        
        def mic_callback(input_data, frame_count, time_info, status_flag):
            audio_queue.put_nowait(input_data)
            return (input_data, pyaudio.paContinue)
        
        async def receiver(ws):
            global __STATUS__
            """Print out the messages received from the server."""
            first_message = True
            first_transcript = True
            transcript = ""

            async for msg in ws:
                res = json.loads(msg)
                if first_message:
                    print(
                        "🟢 (3/5) Successfully receiving Deepgram messages, waiting for finalized transcription..."
                    )
                    first_message = False
                try:
                    # handle local server messages
                    if threading.current_thread().stopped(): 
                        print("----------------- Called stopped -----------------")
                        await ws.send(json.dumps({"type": "CloseStream"}))
                        print(
                            "🟢 (5/5) Successfully closed Deepgram connection, waiting for final transcripts if necessary"
                        )
                        await ws.close()
                        raise Exception("Ending...")
                    if res.get("msg"):
                        print(res["msg"])
                    if res.get("is_final"):
                        transcript = (
                            res.get("channel", {})
                            .get("alternatives", [{}])[0]
                            .get("transcript", "")
                        )
                        if kwargs["timestamps"]:
                            words = res.get("channel", {}).get("alternatives", [{}])[0].get("words", [])
                            start = words[0]["start"] if words else None
                            end = words[-1]["end"] if words else None
                            transcript += " [{} - {}]".format(start, end) if (start and end) else ""
                        if transcript != "":
                            if first_transcript:
                                print("🟢 (4/5) Began receiving transcription")
                                # if using webvtt, print out header
                                if format == "vtt":
                                    print("WEBVTT\n")
                                first_transcript = False
                            if format == "vtt" or format == "srt":
                                transcript = subtitle_formatter(res, format)
                            print(transcript)
                            all_transcripts.append(transcript)

                        # if using the microphone, close stream if user says "goodbye"
                        if method == "mic" and "goodbye" in transcript.lower():
                            await ws.send(json.dumps({"type": "CloseStream"}))
                            print(
                                "🟢 (5/5) Successfully closed Deepgram connection, waiting for final transcripts if necessary"
                            )
                            __STATUS__ = True

                    # handle end of stream
                    if res.get("created"):
                        # save subtitle data if specified
                        if format == "vtt" or format == "srt":
                            data_dir = os.path.abspath(
                                os.path.join(os.path.curdir, "data")
                            )
                            if not os.path.exists(data_dir):
                                os.makedirs(data_dir)

                            transcript_file_path = os.path.abspath(
                                os.path.join(
                                    data_dir,
                                    f"{startTime.strftime('%Y%m%d%H%M')}.{format}",
                                )
                            )
                            with open(transcript_file_path, "w") as f:
                                f.write("".join(all_transcripts))
                            print(f"🟢 Subtitles saved to {transcript_file_path}")

                            # also save mic data if we were live streaming audio
                            # otherwise the wav file will already be saved to disk
                            if method == "mic":
                                wave_file_path = os.path.abspath(
                                    os.path.join(
                                        data_dir,
                                        f"{startTime.strftime('%Y%m%d%H%M')}.wav",
                                    )
                                )
                                wave_file = wave.open(wave_file_path, "wb")
                                wave_file.setnchannels(CHANNELS)
                                wave_file.setsampwidth(SAMPLE_SIZE)
                                wave_file.setframerate(RATE)
                                wave_file.writeframes(b"".join(all_mic_data))
                                wave_file.close()
                                print(f"🟢 Mic audio saved to {wave_file_path}")

                        print(
                            f'🟢 Request finished with a duration of {res["duration"]} seconds. Exiting!'
                        )
                        __STATUS__ = True
                except KeyError:
                    print(f"🔴 ERROR: Received unexpected API response! {msg}")

        # Set up microphone if streaming from mic
        async def microphone():
            audio = pyaudio.PyAudio()
            stream = audio.open(
                format=FORMAT,
                channels=CHANNELS,
                rate=RATE,
                input=True,
                frames_per_buffer=CHUNK,
                stream_callback=mic_callback,
            )

            stream.start_stream()

            global SAMPLE_SIZE
            SAMPLE_SIZE = audio.get_sample_size(FORMAT)

            while stream.is_active():
            # for i in range(0, int(RATE / CHUNK * 2)):
                await asyncio.sleep(0.1)
                if threading.current_thread().stopped(): 
                    print("----------------- Called stopped -----------------")
                    raise Exception("Ending...")
                # data = stream.read(CHUNK)
                # audio_queue.put_nowait(data)
            #     pass

            stream.stop_stream()
            stream.close()

        functions = [
            asyncio.ensure_future(sender(ws)),
            asyncio.ensure_future(receiver(ws)),
        ]

        if method == "mic":
            functions.append(asyncio.ensure_future(microphone()))

        await asyncio.gather(*functions)
        
        if threading.current_thread().stopped(): 
            print("----------------- Called stopped -----------------")
            raise Exception("Ending...")


def validate_input(input):
    if input.lower().startswith("mic"):
        return input

    elif input.lower().endswith("wav"):
        if os.path.exists(input):
            return input

    elif input.lower().startswith("http"):
        return input

    raise argparse.ArgumentTypeError(
        f'{input} is an invalid input. Please enter the path to a WAV file, a valid stream URL, or "mic" to stream from your microphone.'
    )


def validate_format(format):
    if (
        format.lower() == ("text")
        or format.lower() == ("vtt")
        or format.lower() == ("srt")
    ):
        return format

    raise argparse.ArgumentTypeError(
        f'{format} is invalid. Please enter "text", "vtt", or "srt".'
    )

def validate_dg_host(dg_host):
    if (
        # Check that the host is a websocket URL
        dg_host.startswith("wss://")
        or dg_host.startswith("ws://")
    ):
        # Trim trailing slash if necessary
        if dg_host[-1] == '/':
            return dg_host[:-1]
        return dg_host 

    raise argparse.ArgumentTypeError(
            f'{dg_host} is invalid. Please provide a WebSocket URL in the format "{{wss|ws}}://hostname[:port]".'
    )

def parse_args():
    """Parses the command-line arguments."""
    parser = argparse.ArgumentParser(
        description="Submits data to the real-time streaming endpoint."
    )
    parser.add_argument(
        "-k", "--key", default="ce59447b06053a5966b5178374e733d586f210ef",help="YOUR_DEEPGRAM_API_KEY (authorization)"
    )
    parser.add_argument(
        "-i",
        "--input",
        help='Input to stream to Deepgram. Can be "mic" to stream from your microphone (requires pyaudio), the path to a WAV file, or the URL to a direct audio stream. Defaults to the included file preamble.wav',
        nargs="?",
        const=1,
        # default=r"D:\Python\Amir Saleem\deepgram\streaming-test-suite\preamble.wav",
        # default=r"http://stream.live.vc.bbcmedia.co.uk/bbc_world_service",
        default=r"mic",
        type=validate_input,
    )
    parser.add_argument(
        "-m",
        "--model",
        help='Which model to make your request against. Defaults to none specified. See https://developers.deepgram.com/docs/models-overview for all model options.',
        nargs="?",
        const="",
        default="general",
    )
    parser.add_argument(
        "-t",
        "--tier",
        help='Which model tier to make your request against. Defaults to none specified. See https://developers.deepgram.com/docs/tier for all tier options.',
        nargs="?",
        const="",
        default="",
    )
    parser.add_argument(
        "-ts",
        "--timestamps",
        help='Whether to include timestamps in the printed streaming transcript. Defaults to False.',
        nargs="?",
        const=1,
        default=False,
    )
    parser.add_argument(
        "-f",
        "--format",
        help='Format for output. Can be "text" to return plain text, "VTT", or "SRT". If set to VTT or SRT, the audio file and subtitle file will be saved to the data/ directory. Defaults to "text".',
        nargs="?",
        const=1,
        default="text",
        type=validate_format,
    )
    #Parse the host
    parser.add_argument(
        "--host",
        help='Point the test suite at a specific Deepgram URL (useful for on-prem deployments). Takes "{{wss|ws}}://hostname[:port]" as its value. Defaults to "wss://api.deepgram.com".',
        nargs="?",
        const=1,
        default="wss://api.deepgram.com",
        type=validate_dg_host,
    )
    return parser.parse_args()


def main(args):
    """Entrypoint for the example."""
    # Parse the command-line arguments.
    # args = parse_args()
    input = args.input
    format = args.format.lower()
    host = args.host

    try:
        if input.lower().startswith("mic"):
            main_coroutine = asyncio.run(run(args.key, "mic", format, model=args.model, tier=args.tier, host=host, timestamps=args.timestamps))
            if threading.current_thread().stopped(): 
                try:
                    print("----------------- Called stopped -----------------")
                    main_coroutine.stop()
                    return
                except RuntimeError:
                    return

        elif input.lower().endswith("wav"):
            if os.path.exists(input):
                # Open the audio file.
                with wave.open(input, "rb") as fh:
                    (
                        channels,
                        sample_width,
                        sample_rate,
                        num_samples,
                        _,
                        _,
                    ) = fh.getparams()
                    assert sample_width == 2, "WAV data must be 16-bit."
                    data = fh.readframes(num_samples)
                    main_coroutine = asyncio.run(
                        run(
                            args.key,
                            "wav",
                            format,
                            model=args.model,
                            tier=args.tier,
                            data=data,
                            channels=channels,
                            sample_width=sample_width,
                            sample_rate=sample_rate,
                            filepath=args.input,
                            host=host,
                            timestamps=args.timestamps,
                        )
                    )
                    if threading.current_thread().stopped(): 
                        try:
                            print("----------------- Called stopped -----------------")
                            main_coroutine.stop()
                            return
                        except RuntimeError:
                            return
            else:
                raise argparse.ArgumentTypeError(
                    f"🔴 {args.input} is not a valid WAV file."
                )

        elif input.lower().startswith("http"):
            main_coroutine = asyncio.run(run(args.key, "url", format, model=args.model, tier=args.tier, url=input, host=host, timestamps=args.timestamps))
            if threading.current_thread().stopped(): 
                        try:
                            print("----------------- Called stopped -----------------")
                            main_coroutine.stop()
                            return
                        except RuntimeError:
                            return
        else:
            raise argparse.ArgumentTypeError(
                f'🔴 {input} is an invalid input. Please enter the path to a WAV file, a valid stream URL, or "mic" to stream from your microphone.'
            )
    except websockets.exceptions.InvalidStatusCode as e:
        print(f'🔴 ERROR: Could not connect to Deepgram! {e.headers.get("dg-error")}')
        print(
            f'🔴 Please contact Deepgram Support (developers@deepgram.com) with request ID {e.headers.get("dg-request-id")}'
        )
        return
    except websockets.exceptions.ConnectionClosedError as e:
        error_description = f"Unknown websocket error."
        print(
            f"🔴 ERROR: Deepgram connection unexpectedly closed with code {e.code} and payload {e.reason}"
        )

        if e.reason == "DATA-0000":
            error_description = "The payload cannot be decoded as audio. It is either not audio data or is a codec unsupported by Deepgram."
        elif e.reason == "NET-0000":
            error_description = "The service has not transmitted a Text frame to the client within the timeout window. This may indicate an issue internally in Deepgram's systems or could be due to Deepgram not receiving enough audio data to transcribe a frame."
        elif e.reason == "NET-0001":
            error_description = "The service has not received a Binary frame from the client within the timeout window. This may indicate an internal issue in Deepgram's systems, the client's systems, or the network connecting them."

        print(f"🔴 {error_description}")
        # TODO: update with link to streaming troubleshooting page once available
        # print(f'🔴 Refer to our troubleshooting suggestions: ')
        print(
            f"🔴 Please contact Deepgram Support (developers@deepgram.com) with the request ID listed above."
        )
        return

    except websockets.exceptions.ConnectionClosedOK:
        return

    except Exception as e:
        print(f"🔴 ERROR: Something went wrong! {e}")
        return


class StoppableThread(threading.Thread):
    """Thread class with a stop() method. The thread itself has to check
    regularly for the stopped() condition."""

    def __init__(self,  *args, **kwargs):
        super(StoppableThread, self).__init__(*args, **kwargs)
        self._stop_event = threading.Event()

    def stop(self):
        self._stop_event.set()

    def stopped(self):
        return self._stop_event.is_set()

def formThread():
    t = StoppableThread(target=main, args=(args,))
    # add_script_run_ctx(t)
    t.start()
def stopThread():
    for t in threading.enumerate():
        if hasattr(t, "stop"):
            t.stop()
            t.join()
    return

if __name__ == "__main__":
    # sys.exit(main() or 0)
    try:
        args = parse_args()
        button_stop = False
        # button_live = st.button('Live Audio', use_container_width=True)
        col1, col2 = st.columns([1,1])
        with col2:
            if st.button('Stop'): button_stop = True
        with col1:
            if st.button('Live Audio', use_container_width=True):
                pass
            #     # t = StoppableThread(target=main, args=(args,))
            #     # threads.append(t)
            #     formThread()
            #     tsc_placeholder = st.empty()
            #     while not button_stop:
            #         tsc_placeholder.write(''.join(all_transcripts[-len(all_transcripts)//3:]))
            #         time.sleep(0.3)
            # if button_stop:
            #     print("\033[93m"+"INFO: Calling stop on thread")
            #     stopThread()
            upload_btn = st.button('Upload File', use_container_width=True)
            if "upload_btn_state" not in st.session_state:
                st.session_state.upload_btn_state = False
                
            if upload_btn or st.session_state.upload_btn_state:
                st.session_state.upload_btn_state = True 
                uploaded_file = st.file_uploader("Choose and audio file", type=['wav', 'wave', 'mp3'], accept_multiple_files=False)
                # print(uploaded_file.name)
                if uploaded_file is not None:
                    if not os.path.exists("tempDir"): os.mkdir("tempDir")
                    save_pth = f"tempDir/{uploaded_file.name}"
                    with open(save_pth, "wb") as f:
                        f.write(uploaded_file.getbuffer())
                    print("FILE: ",save_pth)
                    args.input = save_pth
                    formThread()
                    tsc_placeholder = st.empty()
                    # while not button_stop:
                    #     tsc_placeholder.write(all_transcripts[-1])
                    #     time.sleep(0.3)
                    with st.spinner('Generating Transcriptions...'):
                        while not __STATUS__: pass
                    st.markdown(''.join(all_transcripts))
            if button_stop:
                print("\033[93m"+"INFO: Calling stop on thread")
                __STATUS__ = True
                stopThread()
                
    except Exception as e:
        print(f"Exception: {e}")
        stopThread()
    except KeyboardInterrupt:
        sys.exit(0)