// Same-origin script (CSP: script-src 'self'): confirm dialogs for forms with data-confirm.
document.addEventListener("submit", function (event) {
  var message = event.target.getAttribute && event.target.getAttribute("data-confirm");
  if (message && !window.confirm(message)) {
    event.preventDefault();
  }
});
