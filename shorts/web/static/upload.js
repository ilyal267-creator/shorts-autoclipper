// Streams the chosen file as the request body so the server can write it straight to disk,
// with a progress bar; then hands over to the settings page for that upload.
(function () {
  const form = document.getElementById("upload");
  if (!form) return;
  const input = form.querySelector("input[type=file]");
  const progress = form.querySelector(".upload-progress");
  const fill = form.querySelector(".bar-fill");
  const status = form.querySelector(".upload-status");
  const error = form.querySelector(".upload-error");
  const maxBytes = Number(form.dataset.maxBytes);

  function fail(message) {
    progress.hidden = true;
    error.textContent = message;
    error.hidden = false;
    input.value = "";
    input.disabled = false;
  }

  input.addEventListener("change", function () {
    const file = input.files[0];
    if (!file) return;
    error.hidden = true;
    if (file.size > maxBytes) return fail("That file is too big for the test. Trim it or export a smaller copy.");

    input.disabled = true;
    progress.hidden = false;
    form.querySelector(".upload-name").textContent = file.name;
    status.textContent = "Uploading…";

    const xhr = new XMLHttpRequest();
    xhr.open("POST", "/api/uploads");
    xhr.setRequestHeader("X-Filename", encodeURIComponent(file.name));
    xhr.upload.onprogress = function (event) {
      if (!event.lengthComputable) return;
      const share = event.loaded / event.total;
      fill.style.width = (share * 100).toFixed(1) + "%";
      status.textContent = share < 1 ? "Uploading… " + Math.floor(share * 100) + "%" : "Checking the video…";
    };
    xhr.onload = function () {
      let body = {};
      try { body = JSON.parse(xhr.responseText); } catch (e) {}
      if (xhr.status === 200 && body.next) return window.location.assign(body.next);
      if (xhr.status === 401) return window.location.assign("/signin");
      fail(body.error || "The upload didn't go through. Try again.");
    };
    xhr.onerror = function () { fail("The connection dropped during the upload. Try again."); };
    xhr.send(file);
  });
})();
