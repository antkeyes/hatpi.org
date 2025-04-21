// /static/lightcurves.js

document.addEventListener('DOMContentLoaded', function() {
  
  // 1) Regenerate each CLI block as HTML (to preserve <span> styling)
  function updateCliCommands() {
    const ra   = document.getElementById('ra').value;
    const dec  = document.getElementById('dec').value;
    const dt   = document.getElementById('date_type').value;
    const dmin = document.getElementById('date_min').value;
    const dmax = document.getElementById('date_max').value;

    // Build the JSON payload
    const payload = `{ \"ra\": \"${ra}\", \"dec\": \"${dec}\", \"date_type\": \"${dt}\", \"date_min\": \"${dmin}\", \"date_max\": \"${dmax}\" }`;

    // JSON example
    document.getElementById('pre-json').innerHTML =
`# JSON
<span class="command">curl</span> <span class="option">-X</span> <span class="value">POST</span> \\
<span class="option">-H</span> <span class="string">"Content-Type: application/json"</span> \\
<span class="option">-d</span> <span class="string">'${payload}'</span> \\
<span class="url">'https://hatpi.org/api/data'</span>`;

    // CSV example
    document.getElementById('pre-csv').innerHTML =
`# CSV
<span class="command">curl</span> <span class="option">-X</span> <span class="value">POST</span> \\
<span class="option">-H</span> <span class="string">"Content-Type: application/json"</span> \\
<span class="option">-d</span> <span class="string">'${payload}'</span> \\
<span class="url">'https://hatpi.org/api/data?format=csv'</span>`;

    // VOTable example
    document.getElementById('pre-votable').innerHTML =
`# VOTable
<span class="command">curl</span> <span class="option">-X</span> <span class="value">POST</span> \\
<span class="option">-H</span> <span class="string">"Content-Type: application/json"</span> \\
<span class="option">-d</span> <span class="string">'${payload}'</span> \\
<span class="url">'https://hatpi.org/api/data?format=votable'</span>`;
  }

  // 2) Wire up live updates on both text inputs and selects
  ['ra', 'dec', 'date_type', 'date_min', 'date_max'].forEach(function(id) {
    const el = document.getElementById(id);
    if (el) {
      el.addEventListener('input', updateCliCommands);
      el.addEventListener('change', updateCliCommands);
    }
  });

  // Seed them on load
  updateCliCommands();

  // 3) Copy‑button logic
  document.querySelectorAll('.copy-button').forEach(function(button) {
    button.addEventListener('click', function() {
      const targetId = button.getAttribute('data-target'); // e.g. "pre-json"
      const pre      = document.getElementById(targetId);
      const text     = pre.textContent.trim();

      navigator.clipboard.writeText(text).then(function() {
        const label = targetId.split('-')[1].toUpperCase();
        button.textContent = `Copied ${label}! ✅`;
        setTimeout(function() {
          button.textContent = `Copy ${label} 📋`;
        }, 1500);
      }).catch(function(err) {
        console.error('Error copying text: ', err);
      });
    });
  });

});
