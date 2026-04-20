import type { Toast as ToastType } from "../types";

interface ToastProps {
  toast: ToastType | null;
  onClose: () => void;
}

export function Toast({ toast, onClose }: ToastProps) {
  if (!toast) return null;
  return (
    <div className="toast" role="status">
      <button
        type="button"
        className="toast-close"
        onClick={onClose}
        aria-label="Close"
      >
        ×
      </button>
      {toast.image_url && (
        <img src={toast.image_url} alt="" className="toast-image" />
      )}
      <div className="toast-body">
        {toast.subtitle && <div className="toast-subtitle">{toast.subtitle}</div>}
        <div className="toast-title">{toast.title}</div>
        <div className="toast-description">{toast.description}</div>
      </div>
    </div>
  );
}
