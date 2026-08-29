(() => {
  // ============================================================================
  // STRING DEFINITIONS
  // ============================================================================
  const translations = {
    en: {
      confirmingPayment: "Confirming your payment...",
      paymentConfirmed: "Payment confirmed!",
      paymentConfirmationPending: "We couldn't confirm your payment yet. Please try again.",
      tryAgain: "Try again",
      returnToCustomSong: "Return to Custom Song",
      paymentNotCompleted: "Payment was not completed.",
      paymentCancelMessage: "Your Custom Song order was not finalized."
    },
    es: {
      confirmingPayment: "Confirmando tu pago...",
      paymentConfirmed: "¡Pago confirmado!",
      paymentConfirmationPending: "Todavía no pudimos confirmar tu pago. Inténtalo de nuevo.",
      tryAgain: "Intentar de nuevo",
      returnToCustomSong: "Volver a Canción Personalizada",
      paymentNotCompleted: "El pago no se completó.",
      paymentCancelMessage: "Tu pedido de Canción Personalizada no se finalizó."
    }
  };

  // ============================================================================
  // LANGUAGE DETECTION
  // ============================================================================
  let language = "en";
  if (typeof document !== "undefined") {
    const detected = (navigator.language || navigator.userLanguage || "").substring(0, 2).toLowerCase();
    if (detected === "es" || detected === "es-es" || detected === "es-mx") {
      language = "es";
    }
    const htmlLang = document.documentElement.lang;
    if (htmlLang && htmlLang.startsWith("es")) {
      language = "es";
    }
  }

  const t = (key) => translations[language][key] || translations.en[key] || key;

  // ============================================================================
  // DOM ELEMENTS
  // ============================================================================
  let isConfirming = false;

  function getElement(id) {
    const el = document.getElementById(id);
    if (!el) {
      console.warn(`Element with id "${id}" not found`);
    }
    return el;
  }

  const statusHeading = getElement("status-heading");
  const statusMessage = getElement("status-message");
  const paymentAmount = getElement("payment-amount");
  const paymentResult = getElement("payment-result");
  const retryButton = getElement("retry-button");
  const returnActions = getElement("return-actions");

  // ============================================================================
  // TOKEN EXTRACTION
  // ============================================================================
  function getToken() {
    if (typeof window === "undefined") return null;
    const params = new URLSearchParams(window.location.search);
    return params.get("token") || null;
  }

  // ============================================================================
  // URL ENCODING
  // ============================================================================
  function encodeToken(token) {
    return encodeURIComponent(token);
  }

  // ============================================================================
  // API CALLS
  // ============================================================================
  async function resolveOrder(token) {
    const encodedToken = encodeToken(token);
    const url = `/api/paypal/orders/resolve?token=${encodedToken}`;
    const response = await fetch(url, {
      method: "GET",
      headers: {
        "Content-Type": "application/json"
      }
    });
    return response;
  }

  async function captureOrder(localOrderId) {
    const url = `/api/paypal/orders/${encodeURIComponent(localOrderId)}/capture`;
    const response = await fetch(url, {
      method: "POST",
      headers: {
        "Content-Type": "application/json"
      },
      body: JSON.stringify({})
    });
    return response;
  }

  // ============================================================================
  // VALIDATION
  // ============================================================================
  function isValidResponse(data) {
    if (!data || typeof data !== "object") return false;
    return (
      data.local_order_id &&
      typeof data.local_order_id === "string" &&
      data.local_order_id.length > 0
    );
  }

  function isValidCaptureResponse(data) {
    if (!data || typeof data !== "object") return false;
    return (
      data.status === "PAID" &&
      data.local_order_id &&
      data.paypal_order_id &&
      data.capture_id &&
      data.amount !== undefined &&
      data.currency !== undefined
    );
  }

  // ============================================================================
  // UI UPDATES
  // ============================================================================
  function setLoading() {
    isConfirming = true;
    if (retryButton) retryButton.disabled = true;
    if (statusHeading) statusHeading.textContent = t("confirmingPayment");
    if (statusMessage) statusMessage.textContent = "";
    if (paymentResult) paymentResult.hidden = true;
  }

  function showSuccess(amount, currency) {
    isConfirming = false;
    if (statusHeading) statusHeading.textContent = t("paymentConfirmed");
    if (statusMessage) statusMessage.textContent = "";
    if (paymentAmount && amount && currency) {
      paymentAmount.textContent = `${amount} ${currency}`;
    }
    if (paymentResult) paymentResult.hidden = false;
    if (retryButton) retryButton.hidden = true;
  }

  function showRecoverableError() {
    isConfirming = false;
    if (statusHeading) statusHeading.textContent = t("paymentConfirmationPending");
    if (statusMessage) statusMessage.textContent = "";
    if (paymentResult) paymentResult.hidden = false;
    if (retryButton) {
      retryButton.hidden = false;
      retryButton.disabled = false;
    }
  }

  function showFatalError(message) {
    isConfirming = false;
    if (statusHeading) statusHeading.textContent = t("paymentConfirmationPending");
    if (statusMessage) statusMessage.textContent = message;
    if (paymentResult) paymentResult.hidden = false;
    if (retryButton) {
      retryButton.hidden = false;
      retryButton.disabled = false;
    }
  }

  // ============================================================================
  // ERROR MESSAGE SANITIZATION
  // ============================================================================
  function getSafeErrorMessage(error) {
    const defaultMessage = t("paymentConfirmationPending");
    if (!error) return defaultMessage;
    if (typeof error === "string") {
      // Sanitize - only allow basic characters
      const sanitized = error.replace(/[<>"'`]/g, "");
      return sanitized.length > 0 ? sanitized : defaultMessage;
    }
    if (error.message) {
      return getSafeErrorMessage(error.message);
    }
    return defaultMessage;
  }

  // ============================================================================
  // MAIN FLOW
  // ============================================================================
  async function confirmPayment() {
    if (isConfirming) return;

    const token = getToken();

    // If no token, show fatal error - cannot resolve
    if (!token) {
      showFatalError(t("paymentConfirmationPending"));
      return;
    }

    setLoading();

    try {
      // Step 1: Resolve token to local_order_id
      const resolveResponse = await resolveOrder(token);

      if (!resolveResponse.ok) {
        // Resolve failed - cannot proceed to Capture
        const errorText = await resolveResponse.text();
        try {
          const errorData = JSON.parse(errorText);
          showRecoverableError();
        } catch (e) {
          showRecoverableError();
        }
        return;
      }

      const resolveData = await resolveResponse.json();

      // Validate resolve response
      if (!isValidResponse(resolveData)) {
        showRecoverableError();
        return;
      }

      const localOrderId = resolveData.local_order_id;

      // Step 2: Capture the order
      const captureResponse = await captureOrder(localOrderId);

      if (!captureResponse.ok) {
        // Capture failed - check status for specific handling
        const status = captureResponse.status;
        const errorText = await captureResponse.text();

        // 409, 503, 502, 500, 404 - all recoverable
        try {
          const errorData = JSON.parse(errorText);
        } catch (e) {
          // Cannot parse, still recoverable
        }
        showRecoverableError();
        return;
      }

      const captureData = await captureResponse.json();

      // Validate capture response structure
      if (!isValidCaptureResponse(captureData)) {
        showRecoverableError();
        return;
      }

      // Success! Payment confirmed
      showSuccess(captureData.amount, captureData.currency);

    } catch (error) {
      // Network or other error
      showRecoverableError();
    }
  }

  // ============================================================================
  // RETRY HANDLER
  // ============================================================================
  function setupRetry() {
    if (retryButton) {
      retryButton.addEventListener("click", async () => {
        if (isConfirming) return;
        await confirmPayment();
      });
    }
  }

  // ============================================================================
  // INITIALIZATION
  // ============================================================================
  if (typeof document !== "undefined") {
    // Set up bilingual strings from data-i18n attributes
    document.querySelectorAll("[data-i18n]").forEach((el) => {
      const key = el.dataset.i18n;
      if (key && translations.en[key]) {
        el.textContent = t(key);
      }
    });

    // Initialize UI
    setLoading();
    setupRetry();

    // Start confirmation flow when page loads
    // Use setTimeout to allow DOM to settle
    setTimeout(() => confirmPayment(), 0);
  }
})();
