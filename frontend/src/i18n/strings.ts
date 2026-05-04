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
    posSub: 'We\u2019ll pull your menu and 30 days of sales — read-only.',
    posAbout: 'OAuth, read-only',
    posNone: 'I don\u2019t use any of these',

    // ── Connecting ──────────────────────────────────────────────────────
    connecting: 'Connecting to',
    permissionPreview: 'We\u2019ll only read menu, orders, and inventory. We never touch your POS settings or charge customers.',

    // ── Found summary ───────────────────────────────────────────────────
    foundTitle: 'Here\u2019s what we found',
    cleanupCta: 'Looks right \u2014 continue',
    notMyResto: 'This isn\u2019t my restaurant',

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
    headline1: 'Arr\u00eatez de deviner',
    headline2: 'quoi commander.',
    sub: 'ReOrderOS se connecte \u00e0 votre POS, apprend votre menu et vous dit exactement quoi acheter \u2014 chaque semaine.',
    timecost: 'Installation : 20 minutes. Essai 14 jours, sans carte.',
    f1t: 'R\u00e9duisez le co\u00fbt de la nourriture 3 \u00e0 8 %',
    f1b: 'Niveaux par IA bas\u00e9s sur vos vraies ventes',
    f2t: 'Bons de commande en un clic',
    f2b: 'Envoyez aux fournisseurs par SMS, courriel ou PDF',
    f3t: 'Con\u00e7u au Canada',
    f3b: 'Conforme PIPEDA / Loi 25. Vos donn\u00e9es restent au Canada.',
    getStarted: 'Commencer',
    haveAccount: 'J\u2019ai d\u00e9j\u00e0 un compte',

    create: 'Cr\u00e9ez votre compte',
    createSub: 'La configuration ne prendra pas longtemps, nous sommes heureux de vous aider!',
    firstName: 'Pr\u00e9nom',
    bizName: 'Nom du restaurant',
    email: 'Courriel',
    pw: 'Mot de passe',
    pwHint: 'Au moins 8 caract\u00e8res.',
    cont: 'Continuer',

    pushTitle: 'Restez au courant de votre inventaire',
    pushSub: 'Soyez averti quand le stock est bas, qu\u2019un BC est confirm\u00e9, ou que les ventes grimpent.',
    pushAllow: 'Autoriser les notifications',
    pushLater: 'Plus tard',

    posTitle: 'Connectez votre POS',
    posSub: 'Nous lirons votre menu et 30 jours de ventes \u2014 en lecture seule.',
    posAbout: 'OAuth, lecture seule',
    posNone: 'Je n\u2019utilise aucun de ceux-ci',

    connecting: 'Connexion \u00e0',
    permissionPreview: 'Nous lirons seulement le menu, les commandes et l\u2019inventaire. Nous ne touchons jamais aux r\u00e9glages POS.',

    foundTitle: 'Voici ce qu\u2019on a trouv\u00e9',
    cleanupCta: 'C\u2019est bon \u2014 continuer',
    notMyResto: 'Ce n\u2019est pas mon restaurant',

    cleanupTitle: 'Cat\u00e9gorisez votre menu',
    cleanupSub: 'Les cat\u00e9gories nous aident \u00e0 regrouper les ventes et pr\u00e9dire les besoins.',
    approveAll: 'Tout approuver',
    advance: 'Continuer',
    needMore: '{have} sur {target} cat\u00e9goris\u00e9s',

    switchLang: 'EN',
  },
} as const;

export type Lang = keyof typeof STRINGS;
export type StringKey = keyof typeof STRINGS['en'];
