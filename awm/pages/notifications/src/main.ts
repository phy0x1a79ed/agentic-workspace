import { mount } from 'svelte';
import '@awm/primitives/tokens.css';
import App from './App.svelte';

const target = document.getElementById('app');
if (!target) throw new Error('notifications: #app not found');

mount(App, { target });
