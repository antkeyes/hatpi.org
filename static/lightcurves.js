// Wait until the page has loaded
document.addEventListener('DOMContentLoaded', function() {
    const copyButton = document.getElementById('copyButton');
    const cliQuery = document.getElementById('cliQuery');
  
    copyButton.addEventListener('click', function() {
      // Use the Clipboard API
      navigator.clipboard.writeText(cliQuery.innerText)
        .then(() => {
          // Optionally, provide user feedback—perhaps change the button text briefly.
          copyButton.innerText = "Copied ✅";
          setTimeout(() => {
            copyButton.innerText = "Copy 📋";
          }, 1500);
        })
        .catch(err => {
          // Fallback or error handling
          console.error('Error copying text: ', err);
        });
    });
  });
  