// i18n strings — EN / FR (Quebec).
// Exported as flat dicts; selected via useLang() hook.

export const STRINGS = {
  en: {
    // ── Welcome ─────────────────────────────────────────────────────────
    headline1: 'Stop guessing',
    headline2: 'what to order.',
    sub: 'ReOrderOS connects to your POS, learns your menu, and tells you exactly what to buy — every week.',
    timecost: 'Setup: 20 minutes. 14-day trial, no card.',
    f1t: 'Cut food cost 3–8%',
    f1b: 'AI-driven par levels based on your real sales',
    f2t: 'One-tap purchase orders',
    f2b: 'Send POs to suppliers via SMS, email, or PDF',
    f3t: 'Built in Canada',
    f3b: 'PIPEDA / Loi 25 compliant. Data stays in Canada.',
    getStarted: 'Get started',
    haveAccount: 'I have an account',

    // ── Account ─────────────────────────────────────────────────────────
    create: 'Create your account',
    createSub: 'Set up wont take long, we are happy to help!',
    firstName: 'First name',
    bizName: 'Restaurant name',
    email: 'Email',
    pw: 'Password',
    pwHint: 'At least 8 characters.',
    cont: 'Continue',

    // ── Push ────────────────────────────────────────────────────────────
    pushTitle: 'Stay on top of your inventory',
    pushSub: 'Get notified when stock runs low, when POs are confirmed, and when sales spike.',
    pushAllow: 'Allow notifications',
    pushLater: 'Maybe later',

    // ── POS picker ──────────────────────────────────────────────────────
    posTitle: 'Connect your POS',
    posSub: 'We pull menu, sales, and inventory. Read-only.',
    posAbout: 'OAuth, read-only',
    posNone: "My POS isn’t here",

    // ── Connecting ──────────────────────────────────────────────────────
    connecting: 'Connecting to',
    permissionPreview: 'We’ll only read menu, orders, and inventory. We never touch your POS settings or charge customers.',

    // ── Found summary ───────────────────────────────────────────────────
    foundTitle: 'Here’s what we found',
    cleanupCta: 'Looks right — continue',
    notMyResto: 'This isn’t my restaurant',

    // ── Cleanup ─────────────────────────────────────────────────────────
    cleanupTitle: 'Categorize your menu',
    cleanupSub: 'Categories help us group sales and predict ingredient needs.',
    approveAll: 'Approve all',
    advance: 'Continue',
    needMore: '{have} of {target} categorized',

    // ── Language toggle ─────────────────────────────────────────────────
    switchLang: 'FR',
  },
  fr: {
    headline1: 'Arrêtez de deviner',
    headline2: 'quoi commander.',
    sub: 'ReOrderOS se connecte à votre POS, apprend votre menu et vous dit exactement quoi acheter — chaque semaine.',
    timecost: 'Installation : 20 minutes. Essai 14 jours, sans carte.',
    f1t: 'Réduisez le coût de la nourriture 3 à 8 %',
    f1b: 'Niveaux par IA basés sur vos vraies ventes',
    f2t: 'Bons de commande en un clic',
    f2b: 'Envoyez aux fournisseurs par SMS, courriel ou PDF',
    f3t: 'Conçu au Canada',
    f3b: 'Conforme PIPEDA / Loi 25. Vos données restent au Canada.',
    getStarted: 'Commencer',
    haveAccount: 'J’ai déjà un compte',

    create: 'Créez votre compte',
    createSub: 'La configuration ne prendra pas longtemps, nous sommes heureux de vous aider!',
    firstName: 'Prénom',
    bizName: 'Nom du restaurant',
    email: 'Courriel',
    pw: 'Mot de passe',
    pwHint: 'Au moins 8 caractères.',
    cont: 'Continuer',

    pushTitle: 'Restez au courant de votre inventaire',
    pushSub: 'Soyez averti quand le stock est bas, qu’un BC est confirmé, ou que les ventes grimpent.',
    pushAllow: 'Autoriser les notifications',
    pushLater: 'Plus tard',

    posTitle: 'Connectez votre POS',
    posSub: 'Nous lisons le menu, les ventes et l’inventaire. Lecture seule.',
    posAbout: 'OAuth, lecture seule',
    posNone: 'Mon POS n’est pas ici',

    connecting: 'Connexion à',
    permissionPreview: 'Nous lirons seulement le menu, les commandes et l’inventaire. Nous ne touchons jamais aux réglages POS.',

    foundTitle: 'Voici ce qu’on a trouvé',
    cleanupCta: 'C’est bon — continuer',
    notMyResto: 'Ce n’est pas mon restaurant',

    cleanupTitle: 'Catégorisez votre menu',
    cleanupSub: 'Les catégories nous aident à regrouper les ventes et prédire les besoins.',
    approveAll: 'Tout approuver',
    advance: 'Continuer',
    needMore: '{have} sur {target} catégorisés',

    switchLang: 'EN',
  },
} as const;

export type Lang = keyof typeof STRINGS;
export type StringKey = keyof typeof STRINGS['en'];
