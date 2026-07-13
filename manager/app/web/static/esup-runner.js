/**
 * @file Shared browser behaviors for the Runner Manager administration pages.
 *
 * This script handles page refresh controls and destructive-action confirmations.
 * Confirmations use a reusable Bootstrap modal when available and fall back to
 * the browser's native confirmation dialog otherwise.
 */

/**
 * Configuration for the shared confirmation dialog.
 *
 * @typedef {Object} ConfirmationOptions
 * @property {string} [title] Dialog title.
 * @property {string} [message] Description of the action to confirm.
 * @property {string} [subject] Optional identifier of the affected item.
 * @property {string} [subjectLabel] Label displayed before the subject.
 * @property {string} [warning] Warning displayed below the action description.
 * @property {string} [cancelLabel] Label of the cancellation button.
 * @property {string} [confirmLabel] Label of the confirmation button.
 */

const refreshBtn = document.getElementById("refreshBtn");
if (refreshBtn) {
  refreshBtn.addEventListener("click", () => {
    window.location.reload();
  });
}

/**
 * Returns the shared confirmation modal, creating it on first use.
 *
 * @returns {HTMLElement} The modal root element attached to the document body.
 */
function getActionConfirmationModal() {
  let modalElement = document.getElementById("actionConfirmationModal");
  if (modalElement) {
    return modalElement;
  }

  modalElement = document.createElement("div");
  modalElement.id = "actionConfirmationModal";
  modalElement.className = "modal fade confirmation-modal";
  modalElement.tabIndex = -1;
  modalElement.setAttribute("aria-labelledby", "actionConfirmationModalLabel");
  modalElement.setAttribute(
    "aria-describedby",
    "actionConfirmationModalMessage actionConfirmationModalWarning",
  );
  modalElement.setAttribute("aria-hidden", "true");
  modalElement.innerHTML = `
    <div class="modal-dialog modal-dialog-centered">
      <div class="modal-content">
        <button type="button" class="btn-close confirmation-modal-close" data-bs-dismiss="modal" aria-label="Cancel"></button>
        <div class="modal-body text-center p-4 p-sm-5 pb-sm-4">
          <div class="confirmation-modal-icon mx-auto mb-3" aria-hidden="true">
            <i class="bi bi-trash3"></i>
          </div>
          <div class="confirmation-modal-kicker mb-2">Destructive action</div>
          <h2 class="modal-title h4 mb-2" id="actionConfirmationModalLabel"></h2>
          <p id="actionConfirmationModalMessage" class="text-body-secondary mb-3"></p>
          <div id="actionConfirmationModalSubject" class="confirmation-modal-subject d-none mb-3">
            <span data-confirm-subject-label>Item</span>
            <code></code>
          </div>
          <div id="actionConfirmationModalWarning" class="confirmation-modal-warning text-start" role="note">
            <i class="bi bi-exclamation-triangle-fill" aria-hidden="true"></i>
            <span></span>
          </div>
        </div>
        <div class="modal-footer confirmation-modal-actions">
          <button type="button" class="btn btn-light" data-confirm-cancel data-bs-dismiss="modal">
            Cancel
          </button>
          <button type="button" class="btn btn-danger shadow-sm" data-confirm-accept>
            <i class="bi bi-trash3 me-2" aria-hidden="true"></i>
            <span data-confirm-accept-label>Delete</span>
          </button>
        </div>
      </div>
    </div>`;
  document.body.appendChild(modalElement);
  return modalElement;
}

/**
 * Displays a confirmation dialog and resolves with the user's decision.
 *
 * @param {ConfirmationOptions} options Dialog content and labels.
 * @returns {Promise<boolean>} Whether the user confirmed the action.
 */
window.esupConfirm = function esupConfirm(options) {
  const title = options.title || "Confirm action";
  const message = options.message || "Do you want to continue?";
  const warning = options.warning || "This action cannot be undone.";

  if (!window.bootstrap || !window.bootstrap.Modal) {
    return Promise.resolve(
      window.confirm(`${title}\n\n${message}\n\n${warning}`),
    );
  }

  const modalElement = getActionConfirmationModal();
  const titleElement = modalElement.querySelector(".modal-title");
  const messageElement = modalElement.querySelector(
    "#actionConfirmationModalMessage",
  );
  const subjectElement = modalElement.querySelector(
    "#actionConfirmationModalSubject",
  );
  const subjectLabelElement = subjectElement.querySelector(
    "[data-confirm-subject-label]",
  );
  const subjectCodeElement = subjectElement.querySelector("code");
  const warningElement = modalElement.querySelector(
    "#actionConfirmationModalWarning span",
  );
  const cancelButton = modalElement.querySelector("[data-confirm-cancel]");
  const confirmButton = modalElement.querySelector("[data-confirm-accept]");
  const confirmLabelElement = confirmButton.querySelector(
    "[data-confirm-accept-label]",
  );

  titleElement.textContent = title;
  messageElement.textContent = message;
  subjectLabelElement.textContent = options.subjectLabel || "Item";
  subjectCodeElement.textContent = options.subject || "";
  subjectElement.classList.toggle("d-none", !options.subject);
  warningElement.textContent = warning;
  cancelButton.textContent = options.cancelLabel || "Cancel";
  confirmLabelElement.textContent = options.confirmLabel || "Confirm";

  return new Promise((resolve) => {
    const modal = window.bootstrap.Modal.getOrCreateInstance(modalElement);
    let accepted = false;

    const handleConfirm = () => {
      accepted = true;
      modal.hide();
    };
    const handleShown = () => cancelButton.focus();
    const handleHidden = () => {
      confirmButton.removeEventListener("click", handleConfirm);
      modalElement.removeEventListener("shown.bs.modal", handleShown);
      resolve(accepted);
    };

    confirmButton.addEventListener("click", handleConfirm);
    modalElement.addEventListener("shown.bs.modal", handleShown, {
      once: true,
    });
    modalElement.addEventListener("hidden.bs.modal", handleHidden, {
      once: true,
    });
    modal.show();
  });
};

document.addEventListener("submit", async (event) => {
  const form = event.target;
  if (
    !(form instanceof HTMLFormElement) ||
    !form.matches("[data-delete-confirm]")
  ) {
    return;
  }

  if (form.dataset.deleteConfirmed === "true") {
    delete form.dataset.deleteConfirmed;
    return;
  }

  event.preventDefault();
  if (form.dataset.deleteConfirmationPending === "true") {
    return;
  }

  form.dataset.deleteConfirmationPending = "true";
  const submitter = event.submitter;
  const confirmed = await window.esupConfirm({
    title: form.dataset.confirmTitle,
    message: form.dataset.confirmMessage,
    subject: form.dataset.confirmSubject,
    subjectLabel: form.dataset.confirmSubjectLabel,
    warning: form.dataset.confirmWarning,
    confirmLabel: form.dataset.confirmLabel,
  });
  delete form.dataset.deleteConfirmationPending;

  if (!confirmed) {
    return;
  }

  form.dataset.deleteConfirmed = "true";
  if (typeof form.requestSubmit === "function") {
    form.requestSubmit(submitter || undefined);
    return;
  }

  HTMLFormElement.prototype.submit.call(form);
});
