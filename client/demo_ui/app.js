const sceneCanvas = document.querySelector("#sceneCanvas");
const sceneCtx = sceneCanvas.getContext("2d");
const blenderFrame = document.querySelector("#blenderFrame");
const blenderFallback = document.querySelector("#blenderFallback");
const video = document.querySelector("#cameraVideo");
const handCanvas = document.querySelector("#handCanvas");
const handCtx = handCanvas.getContext("2d");
const cameraButton = document.querySelector("#cameraButton");
const cameraFallback = document.querySelector("#cameraFallback");
const terminalOutput = document.querySelector("#terminalOutput");
const terminalForm = document.querySelector("#terminalForm");
const terminalInput = document.querySelector("#terminalInput");
const voiceButton = document.querySelector("#voiceButton");
const clearButton = document.querySelector("#clearButton");
const gestureLabel = document.querySelector("#gestureLabel");
const confidenceLabel = document.querySelector("#confidenceLabel");
const routeLabel = document.querySelector("#routeLabel");
const gestureChips = [...document.querySelectorAll(".gesture-chip")];

let stream = null;
let handLandmarker = null;
let lastVideoTime = -1;
let activeMode = "point";
let syntheticGestureIndex = 0;
let blenderScreenConnected = false;
let commandRunning = false;
let recognition = null;
let listening = false;

const syntheticGestures = ["Neutral", "Point", "Pinch", "Peace", "Open palm"];
const handConnections = [
  [0, 1], [1, 2], [2, 3], [3, 4],
  [0, 5], [5, 6], [6, 7], [7, 8],
  [5, 9], [9, 10], [10, 11], [11, 12],
  [9, 13], [13, 14], [14, 15], [15, 16],
  [13, 17], [17, 18], [18, 19], [19, 20], [0, 17],
];

function resizeCanvas(canvas, context) {
  const rect = canvas.getBoundingClientRect();
  const scale = window.devicePixelRatio || 1;
  const width = Math.max(1, Math.floor(rect.width * scale));
  const height = Math.max(1, Math.floor(rect.height * scale));
  if (canvas.width !== width || canvas.height !== height) {
    canvas.width = width;
    canvas.height = height;
  }
  context.setTransform(scale, 0, 0, scale, 0, 0);
  return rect;
}

function drawScene(time = 0) {
  const rect = resizeCanvas(sceneCanvas, sceneCtx);
  sceneCtx.clearRect(0, 0, rect.width, rect.height);
  sceneCtx.fillStyle = "#121312";
  sceneCtx.fillRect(0, 0, rect.width, rect.height);

  const horizon = rect.height * 0.66;
  sceneCtx.strokeStyle = "rgba(255, 255, 255, 0.08)";
  sceneCtx.lineWidth = 1;
  for (let i = -10; i <= 10; i += 1) {
    const x = rect.width * 0.5 + i * 48;
    sceneCtx.beginPath();
    sceneCtx.moveTo(x, horizon);
    sceneCtx.lineTo(rect.width * 0.5 + i * 120, rect.height);
    sceneCtx.stroke();
  }
  for (let i = 0; i < 12; i += 1) {
    const y = horizon + i * i * 3.8;
    sceneCtx.beginPath();
    sceneCtx.moveTo(0, y);
    sceneCtx.lineTo(rect.width, y);
    sceneCtx.stroke();
  }

  const t = time * 0.001;
  const centerX = rect.width * 0.42;
  const centerY = rect.height * 0.47 + Math.sin(t) * 8;
  const height = Math.min(rect.height * 0.46, 360);
  const width = height * 0.2;
  const tilt = Math.sin(t * 0.65) * 16;

  sceneCtx.save();
  sceneCtx.translate(centerX, centerY);
  sceneCtx.rotate((tilt * Math.PI) / 180);
  sceneCtx.strokeStyle = "rgba(110, 216, 223, 0.82)";
  sceneCtx.fillStyle = "rgba(110, 216, 223, 0.08)";
  sceneCtx.lineWidth = 2;

  sceneCtx.beginPath();
  sceneCtx.moveTo(0, -height * 0.52);
  sceneCtx.quadraticCurveTo(width, -height * 0.44, width, -height * 0.3);
  sceneCtx.lineTo(width, height * 0.36);
  sceneCtx.lineTo(width * 0.52, height * 0.48);
  sceneCtx.lineTo(-width * 0.52, height * 0.48);
  sceneCtx.lineTo(-width, height * 0.36);
  sceneCtx.lineTo(-width, -height * 0.3);
  sceneCtx.quadraticCurveTo(-width, -height * 0.44, 0, -height * 0.52);
  sceneCtx.closePath();
  sceneCtx.fill();
  sceneCtx.stroke();

  sceneCtx.strokeStyle = "rgba(229, 183, 107, 0.88)";
  for (const side of [-1, 1]) {
    sceneCtx.beginPath();
    sceneCtx.moveTo(side * width * 0.92, height * 0.18);
    sceneCtx.lineTo(side * width * 2.2, height * 0.38);
    sceneCtx.lineTo(side * width * 1.2, height * 0.42);
    sceneCtx.stroke();
  }

  sceneCtx.strokeStyle = "rgba(244, 241, 234, 0.42)";
  for (let y = -height * 0.18; y <= height * 0.28; y += height * 0.115) {
    sceneCtx.beginPath();
    sceneCtx.moveTo(-width * 0.92, y);
    sceneCtx.lineTo(width * 0.92, y);
    sceneCtx.stroke();
  }
  sceneCtx.restore();

  requestAnimationFrame(drawScene);
}

function logLine(message, tone = "info") {
  const now = new Date();
  const stamp = now.toLocaleTimeString([], { hour12: false });
  const line = document.createElement("div");
  line.className = "terminal-line";
  line.innerHTML = `<span class="time">${stamp}</span> <span class="${tone}">${escapeHtml(message)}</span>`;
  terminalOutput.appendChild(line);
  terminalOutput.scrollTop = terminalOutput.scrollHeight;
}

function escapeHtml(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}

function terminalBoot() {
  logLine("Forge UI booted", "ok");
  logLine('Wake phrase: "Hey Travis"', "info");
  logLine("Voice route: direct Text2Blender", "info");
  logLine("Blender screen polling started", "info");
  logLine("Gesture overlay waiting for camera", "warn");
}

async function runTerminalCommand(command, source = "typed") {
  const text = command.trim();
  if (!text) return;
  logLine(`${source === "voice" ? "voice" : "prompt"}> ${text}`, "ok");
  if (commandRunning) {
    logLine("Another command is still running", "warn");
    return;
  }

  commandRunning = true;
  routeLabel.textContent = "Running Forge command";
  try {
    const response = await fetch("/api/command", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ command: text, source }),
    });
    const payload = await response.json();
    if (payload.mapped_command && payload.mapped_command !== payload.command) {
      logLine(`mapped> ${payload.mapped_command}`, "info");
    }
    if (payload.output) {
      for (const line of payload.output.split("\n")) {
        if (line.trim()) logLine(line, payload.ok ? "info" : "warn");
      }
    }
    logLine(payload.ok ? "command complete" : `command failed (${payload.exit_code})`, payload.ok ? "ok" : "warn");
  } catch (error) {
    logLine(`command request failed: ${error.message}`, "warn");
  } finally {
    commandRunning = false;
    routeLabel.textContent = blenderScreenConnected ? "Live Blender screen connected" : "UI preview mode";
  }
}

function startVoiceCommand() {
  const Recognition = window.SpeechRecognition || window.webkitSpeechRecognition;
  if (!Recognition) {
    logLine("Browser speech recognition is unavailable. Type the command instead.", "warn");
    return;
  }
  if (listening && recognition) {
    recognition.stop();
    return;
  }

  recognition = new Recognition();
  recognition.lang = "en-US";
  recognition.continuous = false;
  recognition.interimResults = false;
  recognition.maxAlternatives = 1;
  listening = true;
  voiceButton.classList.add("listening");
  routeLabel.textContent = "Listening for command";
  logLine("Listening for voice command...", "info");

  recognition.onresult = (event) => {
    const transcript = event.results?.[0]?.[0]?.transcript || "";
    terminalInput.value = transcript;
    logLine(`heard> ${transcript}`, "ok");
    runTerminalCommand(transcript, "voice");
  };
  recognition.onerror = (event) => {
    logLine(`speech recognition failed: ${event.error || "unknown error"}`, "warn");
  };
  recognition.onend = () => {
    listening = false;
    voiceButton.classList.remove("listening");
    routeLabel.textContent = blenderScreenConnected ? "Live Blender screen connected" : "UI preview mode";
  };
  recognition.start();
}

async function pollBlenderScreen() {
  try {
    const response = await fetch(`/api/blender/screenshot?t=${Date.now()}`, {
      cache: "no-store",
    });
    const payload = await response.json();
    if (payload.ok && payload.data_url) {
      blenderFrame.src = payload.data_url;
      blenderFrame.classList.add("active");
      blenderFallback.classList.add("hidden");
      routeLabel.textContent = "Live Blender screen connected";
      if (!blenderScreenConnected) {
        logLine("Blender screen connected", "ok");
      }
      blenderScreenConnected = true;
      setTimeout(pollBlenderScreen, 1200);
      return;
    }

    handleBlenderScreenError(payload.error || "Blender screen unavailable");
  } catch (error) {
    handleBlenderScreenError(error.message || "Blender screen unavailable");
  }
  setTimeout(pollBlenderScreen, 3000);
}

function handleBlenderScreenError(message) {
  blenderFrame.classList.remove("active");
  blenderFallback.classList.remove("hidden");
  blenderFallback.textContent = "Waiting for Blender screen";
  routeLabel.textContent = "UI preview mode";
  if (blenderScreenConnected) {
    logLine(`Blender screen disconnected: ${message}`, "warn");
  }
  blenderScreenConnected = false;
}

async function startCamera() {
  if (stream) {
    for (const track of stream.getTracks()) track.stop();
    stream = null;
    video.srcObject = null;
    cameraFallback.classList.remove("hidden");
    cameraButton.title = "Start camera";
    cameraButton.setAttribute("aria-label", "Start camera");
    gestureLabel.textContent = "Neutral";
    confidenceLabel.textContent = "--";
    logLine("Camera stopped", "warn");
    return;
  }

  try {
    stream = await navigator.mediaDevices.getUserMedia({
      video: { width: 960, height: 720, facingMode: "user" },
      audio: false,
    });
  } catch (error) {
    cameraFallback.textContent = "Camera blocked";
    logLine(`Camera unavailable: ${error.message}`, "warn");
    return;
  }

  video.srcObject = stream;
  cameraFallback.classList.add("hidden");
  cameraButton.title = "Stop camera";
  cameraButton.setAttribute("aria-label", "Stop camera");
  logLine("Camera preview started", "ok");
  await loadHandLandmarker();
  requestAnimationFrame(trackHands);
}

async function loadHandLandmarker() {
  if (handLandmarker) return;
  try {
    const vision = await import("https://cdn.jsdelivr.net/npm/@mediapipe/tasks-vision@0.10.14/vision_bundle.mjs");
    const resolver = await vision.FilesetResolver.forVisionTasks(
      "https://cdn.jsdelivr.net/npm/@mediapipe/tasks-vision@0.10.14/wasm",
    );
    handLandmarker = await vision.HandLandmarker.createFromOptions(resolver, {
      baseOptions: {
        modelAssetPath: "/models/hand_landmarker.task",
        delegate: "GPU",
      },
      runningMode: "VIDEO",
      numHands: 1,
    });
    logLine("MediaPipe hand tracking ready", "ok");
  } catch (error) {
    handLandmarker = null;
    logLine("Hand tracking runtime unavailable; showing gesture simulation", "warn");
    drawSyntheticHand();
  }
}

function trackHands() {
  const rect = resizeCanvas(handCanvas, handCtx);
  handCtx.clearRect(0, 0, rect.width, rect.height);

  if (!stream || video.readyState < 2) {
    requestAnimationFrame(trackHands);
    return;
  }

  if (!handLandmarker) {
    drawSyntheticHand();
    requestAnimationFrame(trackHands);
    return;
  }

  if (video.currentTime !== lastVideoTime) {
    lastVideoTime = video.currentTime;
    const result = handLandmarker.detectForVideo(video, performance.now());
    const landmarks = result.landmarks?.[0];
    if (landmarks) {
      drawLandmarks(landmarks, rect);
      const gesture = classifyGesture(landmarks);
      gestureLabel.textContent = gesture;
      confidenceLabel.textContent = "Live";
      routeLabel.textContent = `${gesture} gesture detected`;
    } else {
      gestureLabel.textContent = "Neutral";
      confidenceLabel.textContent = "--";
    }
  }
  requestAnimationFrame(trackHands);
}

function drawLandmarks(landmarks, rect) {
  handCtx.save();
  handCtx.scale(-1, 1);
  handCtx.translate(-rect.width, 0);
  handCtx.lineWidth = 2;
  handCtx.strokeStyle = "rgba(110, 216, 223, 0.9)";
  handCtx.fillStyle = "rgba(143, 227, 106, 0.95)";
  for (const [a, b] of handConnections) {
    const pa = landmarks[a];
    const pb = landmarks[b];
    handCtx.beginPath();
    handCtx.moveTo(pa.x * rect.width, pa.y * rect.height);
    handCtx.lineTo(pb.x * rect.width, pb.y * rect.height);
    handCtx.stroke();
  }
  for (const point of landmarks) {
    handCtx.beginPath();
    handCtx.arc(point.x * rect.width, point.y * rect.height, 3.5, 0, Math.PI * 2);
    handCtx.fill();
  }
  handCtx.restore();
}

function classifyGesture(landmarks) {
  const extended = [8, 12, 16, 20].map((tip) => landmarks[tip].y < landmarks[tip - 2].y);
  const thumbIndexDistance = distance(landmarks[4], landmarks[8]);
  if (thumbIndexDistance < 0.065) return "Pinch";
  if (extended[0] && extended[1] && !extended[2] && !extended[3]) return "Peace";
  if (extended.every(Boolean)) return "Open palm";
  if (extended[0] && !extended[1] && !extended[2] && !extended[3]) return "Point";
  return "Neutral";
}

function distance(a, b) {
  const dx = a.x - b.x;
  const dy = a.y - b.y;
  return Math.hypot(dx, dy);
}

function drawSyntheticHand() {
  const rect = resizeCanvas(handCanvas, handCtx);
  handCtx.clearRect(0, 0, rect.width, rect.height);
  const time = performance.now() * 0.001;
  const points = [
    [0.52, 0.72], [0.46, 0.62], [0.42, 0.52], [0.39, 0.44], [0.36, 0.38],
    [0.51, 0.58], [0.50, 0.45], [0.49, 0.32], [0.49, 0.22],
    [0.57, 0.57], [0.59, 0.44], [0.61, 0.32], [0.62, 0.22],
    [0.62, 0.60], [0.66, 0.52], [0.69, 0.47], [0.72, 0.43],
    [0.66, 0.65], [0.70, 0.59], [0.74, 0.55], [0.78, 0.52],
  ].map(([x, y]) => ({
    x: x + Math.sin(time + x * 5) * 0.012,
    y: y + Math.cos(time + y * 5) * 0.012,
  }));
  drawLandmarks(points, rect);
  if (Math.floor(time) % 3 === 0) {
    syntheticGestureIndex = (syntheticGestureIndex + 1) % syntheticGestures.length;
  }
  gestureLabel.textContent = syntheticGestures[syntheticGestureIndex];
  confidenceLabel.textContent = "Demo";
}

cameraButton.addEventListener("click", startCamera);

terminalForm.addEventListener("submit", (event) => {
  event.preventDefault();
  runTerminalCommand(terminalInput.value, "typed");
});

voiceButton.addEventListener("click", startVoiceCommand);

clearButton.addEventListener("click", () => {
  terminalOutput.textContent = "";
  terminalBoot();
});

gestureChips.forEach((chip) => {
  chip.addEventListener("click", () => {
    activeMode = chip.dataset.mode;
    gestureChips.forEach((item) => item.classList.toggle("active", item === chip));
    routeLabel.textContent = `${activeMode} mode selected`;
    logLine(`Gesture mode: ${activeMode}`, "info");
  });
});

window.addEventListener("resize", () => {
  resizeCanvas(sceneCanvas, sceneCtx);
  resizeCanvas(handCanvas, handCtx);
});

terminalBoot();
requestAnimationFrame(drawScene);
pollBlenderScreen();
