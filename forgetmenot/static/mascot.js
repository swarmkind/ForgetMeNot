"use strict";

function lensState({state, listening = false, submitting = false, savedId = null, error = "", offline = false}) {
  if (submitting) return {mode:"locking", status:"ACQUIRING FOCUS", title:"Consider it my concern.", detail:"Securing your objective."};
  if (listening) return {mode:"listening", status:"INPUT CHANNEL OPEN", title:"I’m listening.", detail:"Take your time. I’m paying attention."};
  if (offline || error) return {mode:"attention", status:"ATTENTION REQUIRED", title:"A moment of your attention.", detail:error || "The server connection is unavailable. Your draft is still here."};
  if (!state) return {mode:"standby", status:"STANDING BY", title:"At your attention.", detail:"Give me something worth remembering."};
  if (state.worker.error) return {mode:"attention", status:"WORKER NEEDS ATTENTION", title:"Something needs a closer look.", detail:state.worker.error};
  if (!state.worker.running) return {mode:"offline", status:"WORKER OFFLINE", title:"Waiting for the system.", detail:"The background worker is offline. Checks are paused."};
  const active = state.watches.filter((watch) => watch.status === "active");
  if (active.some((watch) => watch.checking)) return {mode:"searching", status:"FOCUS ENGAGED", title:"Nothing else on my mind.", detail:"Checking the evidence against your objective."};
  const saved = active.find((watch) => watch.id === savedId);
  if (!state.settings.model.id) return {mode:saved ? "locked" : "standby", status:"AWAITING MODEL", title:saved ? "Objective secured." : "Ready when you are.", detail:saved ? "Your watch is saved. Connect a model to begin checking." : "Connect an account and choose a model to begin."};
  if (active.some((watch) => watch.error)) return {mode:"attention", status:"CHECK NEEDS ATTENTION", title:"Something needs a closer look.", detail:"A check was inconclusive. Review the watch for details."};
  if (active.length) return {mode:"locked", status:"OBJECTIVE HELD", title:"It has my full attention.", detail:"Your next check is scheduled. There’s no need to keep checking."};
  if (state.inbox.notifications.some((item) => !item.seen && item.kind === "match")) return {mode:"found", status:"MATCH CONFIRMED", title:"I think you’ll want to see this.", detail:"A finding and its evidence are waiting in your inbox."};
  return {mode:"standby", status:"STANDING BY", title:"At your attention.", detail:"Give me something worth remembering."};
}

function submitsObjective(event) {
  return event.key === "Enter" && !event.shiftKey && !event.isComposing && event.keyCode !== 229;
}

if (typeof module !== "undefined") module.exports = {lensState, submitsObjective};
