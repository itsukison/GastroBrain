/** Recall Output Media supplies room sound as the default microphone. */
export async function openRecallAudio() {
  const mediaStream = await navigator.mediaDevices.getUserMedia({ audio: true });
  const audioElement = document.createElement("audio");
  audioElement.autoplay = true;
  document.body.appendChild(audioElement);
  return { mediaStream, audioElement };
}
