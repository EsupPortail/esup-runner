/* Live monitoring refresh, broadcaster copying and shared action confirmations. */
(() => {
  const toggle = document.getElementById('live-refresh-toggle');
  const status = document.getElementById('live-refresh-status');
  if (!toggle || !status) return;
  let paused = false;
  let submitting = false;
  let copying = false;
  const update = () => {
    toggle.textContent = paused ? 'Resume refresh' : 'Pause refresh';
    toggle.setAttribute('aria-pressed', String(paused));
    status.textContent = paused
      ? 'Refresh paused.'
      : 'Refresh every 15 seconds; held while editing filters or reading details.';
  };
  toggle.addEventListener('click', () => {
    paused = !paused;
    update();
  });
  const copyStatus = document.getElementById('live-source-copy-status');
  document.querySelectorAll('[data-copy-live-source]').forEach(button => {
    button.addEventListener('click', async () => {
      if (copying) return;
      const source = document.getElementById(`live-source-id-${button.dataset.copyLiveSource}`);
      if (!source) return;
      copying = true;
      try {
        await window.navigator.clipboard.writeText(button.dataset.copyLiveSource);
        copyStatus.textContent = 'Broadcaster name copied to clipboard.';
      } catch {
        source.focus();
        const selection = window.getSelection();
        const range = document.createRange();
        range.selectNodeContents(source);
        selection.removeAllRanges();
        selection.addRange(range);
        paused = true;
        update();
        copyStatus.textContent = 'Copy unavailable. The broadcaster name is selected; copy it manually.';
      } finally {
        copying = false;
      }
    });
  });
  document.querySelectorAll('[data-live-action]').forEach(form => {
    form.addEventListener('submit', async event => {
      event.preventDefault();
      if (submitting) return;
      // Hold refresh while the shared confirmation dialog is open.
      submitting = true;
      const restarting = form.dataset.liveAction === 'restart';
      const confirmed = await window.esupConfirm({
        action: restarting ? 'restart' : 'stop',
        title: restarting ? 'Restart this Live session?' : 'Stop this Live session?',
        message: restarting
          ? 'Create a new HLS session from the RTMP source that is still publishing.'
          : 'End playback and release the runner after cleanup.',
        subject: `${form.dataset.liveSource} · ${form.dataset.liveId}`,
        subjectLabel: 'Live session',
        warning: restarting
          ? 'The RTMP source must be active and a Live runner available. The new session will have a new HLS URL.'
          : 'The HLS files will be removed. If the encoder keeps publishing, use Restart session after cleanup to resume playback.',
        confirmLabel: restarting ? 'Restart session' : 'Stop session',
      });
      if (!confirmed) {
        submitting = false;
        return;
      }
      form.querySelector('button').disabled = true;
      HTMLFormElement.prototype.submit.call(form);
    });
  });
  window.setInterval(() => {
    const editing = ['INPUT', 'SELECT', 'TEXTAREA'].includes(document.activeElement?.tagName);
    if (!paused && !submitting && !copying && !document.hidden && !editing && !document.querySelector('details[open]')) {
      window.location.reload();
    }
  }, 15000);
  update();
})();
