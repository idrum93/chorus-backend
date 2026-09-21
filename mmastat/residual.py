"""Model the market residual instead of the outcome.

The mistake in the standalone approach: we fit a model to predict who wins,
then checked afterwards whether it added anything to the closing line. That
makes "what does the market miss" a diagnostic rather than the objective, and
it wastes most of the model's capacity re-deriving things the line already
knows — we measured that 51% of the line is reproducible from these features,
so roughly half of what the standalone model learns is redundant by
construction.

Here logit(p_market) enters as a FIXED OFFSET with its coefficient pinned at
1.0, so the fitted coefficients answer exactly one question: given the price,
what does this feature still tell you? A coefficient that survives here is a
genuine market inefficiency. A coefficient of zero means the line already
prices it.

sklearn has no offset support, so the likelihood is minimised directly. It is a
plain L2-penalised logistic on top of an offset — 30 lines and no dependency.
"""
import numpy as np
import pandas as pd
from scipy.optimize import minimize


def _nll_and_grad(beta, Xz, y, offset, l2):
    z = offset + Xz @ beta
    # logaddexp form is stable for large |z|
    nll = np.sum(np.logaddexp(0.0, z) - y * z) + 0.5 * l2 * beta @ beta
    resid = 1.0 / (1.0 + np.exp(-z)) - y
    grad = Xz.T @ resid + l2 * beta
    return nll, grad


class OffsetLogit:
    """P(win) = sigmoid(logit(p_market) + X.beta)."""

    def __init__(self, l2=25.0):
        self.l2 = l2

    def fit(self, Xz, y, p_market):
        off = _logit(p_market)
        b0 = np.zeros(Xz.shape[1])
        r = minimize(_nll_and_grad, b0, args=(Xz, np.asarray(y, float), off, self.l2),
                     jac=True, method="L-BFGS-B")
        self.beta = r.x
        self.converged = bool(r.success)
        return self

    def predict_proba(self, Xz, p_market):
        z = _logit(p_market) + Xz @ self.beta
        return 1.0 / (1.0 + np.exp(-z))


def _logit(q):
    q = np.clip(np.asarray(q, float), 1e-6, 1 - 1e-6)
    return np.log(q / (1 - q))


def bootstrap_coefs(Xz, y, p_market, l2=25.0, n=600, seed=0):
    """Coefficient CIs by resampling. With ~2,000 fights and a market offset
    soaking up most of the signal, the remaining coefficients are small and
    their intervals are what decides whether anything real is there."""
    rng = np.random.default_rng(seed)
    N = len(y)
    out = []
    y = np.asarray(y, float)
    pm = np.asarray(p_market, float)
    for _ in range(n):
        i = rng.integers(0, N, N)
        try:
            out.append(OffsetLogit(l2).fit(Xz[i], y[i], pm[i]).beta)
        except Exception:
            continue
    return np.array(out)


def report(feature_names, beta, boot):
    lo = np.percentile(boot, 2.5, axis=0)
    hi = np.percentile(boot, 97.5, axis=0)
    # share of resamples on the same side of zero as the point estimate
    same = np.mean(np.sign(boot) == np.sign(beta), axis=0)
    df = pd.DataFrame({"feature": feature_names, "beta": beta,
                       "ci_lo": lo, "ci_hi": hi, "consistency": same})
    df["crosses_zero"] = (df.ci_lo < 0) & (df.ci_hi > 0)
    return df.reindex(df.beta.abs().sort_values(ascending=False).index)
