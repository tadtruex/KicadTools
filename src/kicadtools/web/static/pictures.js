// Picture files for the page's .dropzone (templates/_dropzone.html): image
// files dropped anywhere on the page, or pasted, go into its file input. With
// data-autosubmit (the item page) they are sent at once; otherwise (quick
// add) they are added to the files already chosen and go with the form.
(function () {
  "use strict";

  function hasFiles(e) {
    return e.dataTransfer && Array.prototype.indexOf.call(e.dataTransfer.types, "Files") >= 0;
  }

  function setup() {
    var zone = document.querySelector(".dropzone");
    if (!zone) return;
    var input = zone.querySelector("input[type=file]");
    var chosen = zone.querySelector(".chosen");
    var auto = zone.hasAttribute("data-autosubmit");
    var depth = 0;

    function send() {
      if (!auto || !input.files.length) return;
      zone.classList.add("busy");
      if (chosen) chosen.textContent = "Adding " + input.files.length + " picture" +
        (input.files.length === 1 ? "" : "s") + "…";
      input.form.submit();
    }

    function show() {
      if (!chosen || auto) return;
      var names = Array.prototype.map.call(input.files, function (f) { return f.name; });
      chosen.textContent = names.length ? names.length + " to add: " + names.join(", ") : "";
    }

    function take(files) {
      var list = new DataTransfer();
      if (!auto) Array.prototype.forEach.call(input.files, function (f) { list.items.add(f); });
      Array.prototype.forEach.call(files, function (f) {
        if (!f.type || f.type.indexOf("image/") === 0) list.items.add(f);
      });
      input.files = list.files;
      show();
      send();
    }

    input.addEventListener("change", function () { show(); send(); });
    document.addEventListener("dragenter", function (e) {
      if (!hasFiles(e)) return;
      depth += 1;
      zone.classList.add("over");
    });
    document.addEventListener("dragleave", function (e) {
      if (!hasFiles(e)) return;
      depth -= 1;
      if (depth <= 0) { depth = 0; zone.classList.remove("over"); }
    });
    document.addEventListener("dragover", function (e) {
      if (hasFiles(e)) e.preventDefault();
    });
    document.addEventListener("drop", function (e) {
      if (!hasFiles(e)) return;
      e.preventDefault();
      depth = 0;
      zone.classList.remove("over");
      take(e.dataTransfer.files);
    });
    document.addEventListener("paste", function (e) {
      var files = e.clipboardData && e.clipboardData.files;
      if (!files || !files.length) return;
      e.preventDefault();
      take(files);
    });
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", setup);
  else setup();
})();
