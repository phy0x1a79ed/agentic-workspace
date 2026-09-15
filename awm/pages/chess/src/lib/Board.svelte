<script lang="ts">
  // cm-chessboard wrapper. Move input is validated against `legalMoves` from the
  // realm, so the page carries no chess rules of its own.
  import { onMount } from 'svelte';
  // @ts-ignore -- cm-chessboard ships ES modules without type declarations
  import { Chessboard, COLOR, INPUT_EVENT_TYPE, BORDER_TYPE } from 'cm-chessboard/src/Chessboard.js';
  // @ts-ignore
  import { Markers, MARKER_TYPE } from 'cm-chessboard/src/extensions/markers/Markers.js';
  // @ts-ignore
  import { PromotionDialog, PROMOTION_DIALOG_RESULT_TYPE } from 'cm-chessboard/src/extensions/promotion-dialog/PromotionDialog.js';
  import 'cm-chessboard/assets/chessboard.css';
  import 'cm-chessboard/assets/extensions/markers/markers.css';
  import 'cm-chessboard/assets/extensions/promotion-dialog/promotion-dialog.css';
  import piecesUrl from 'cm-chessboard/assets/pieces/standard.svg?url';
  import markersUrl from 'cm-chessboard/assets/extensions/markers/markers.svg?url';
  import type { Color } from './api';

  interface Props {
    fen: string;
    orientation?: Color;
    lastMove?: string | null;
    legalMoves?: string[];
    inputColor?: Color | null;
    onmove?: (uci: string) => void;
  }
  let { fen, orientation = 'white', lastMove = null, legalMoves = [], inputColor = null,
        onmove }: Props = $props();

  let el: HTMLDivElement;
  let board = $state.raw<any>(null);

  const abs = (url: string) => new URL(url, window.location.href).href;
  const toColor = (c: Color) => (c === 'black' ? COLOR.black : COLOR.white);

  onMount(() => {
    const created = new Chessboard(el, {
      position: fen,
      orientation: toColor(orientation),
      assetsUrl: abs('./'),
      style: {
        cssClass: 'default',
        showCoordinates: true,
        borderType: BORDER_TYPE.frame,
        aspectRatio: 1,
        pieces: { type: 'svgSprite', file: abs(piecesUrl), tileSize: 40 },
        animationDuration: 200,
      },
      extensions: [
        { class: Markers, props: { sprite: abs(markersUrl), autoMarkers: MARKER_TYPE.square } },
        { class: PromotionDialog },
      ],
    });
    board = created;
    return () => created.destroy();
  });

  export function redraw() {
    board?.setPosition(fen, true);
  }

  $effect(() => {
    board?.setPosition(fen, true);
  });

  $effect(() => {
    board?.setOrientation(toColor(orientation));
  });

  $effect(() => {
    if (!board) return;
    board.removeMarkers(MARKER_TYPE.frame);
    if (lastMove) {
      board.addMarker(MARKER_TYPE.frame, lastMove.slice(0, 2));
      board.addMarker(MARKER_TYPE.frame, lastMove.slice(2, 4));
    }
  });

  $effect(() => {
    if (!board) return;
    const moves = legalMoves;
    const color = inputColor;
    board.disableMoveInput();
    if (color && moves.length) board.enableMoveInput((e: any) => onInput(e, moves, color), toColor(color));
  });

  function onInput(event: any, moves: string[], color: Color) {
    switch (event.type) {
      case INPUT_EVENT_TYPE.moveInputStarted: {
        const targets = moves.filter((m) => m.startsWith(event.squareFrom)).map((m) => m.slice(2, 4));
        board.removeMarkers(MARKER_TYPE.dot);
        for (const square of targets) board.addMarker(MARKER_TYPE.dot, square);
        return targets.length > 0;
      }
      case INPUT_EVENT_TYPE.validateMoveInput: {
        // A completion animation keeps the board busy, and a bot replying at once
        // would then swallow the player's next pick-up.
        event.animate = false;
        board.removeMarkers(MARKER_TYPE.dot);
        const uci = `${event.squareFrom}${event.squareTo}`;
        const matching = moves.filter((m) => m.slice(0, 4) === uci);
        if (!matching.length) return false;
        if (matching[0].length === 5) {
          board.showPromotionDialog(event.squareTo, toColor(color), (result: any) => {
            if (result.type === PROMOTION_DIALOG_RESULT_TYPE.pieceSelected) {
              onmove?.(uci + String(result.piece).charAt(1));
            } else {
              board.setPosition(fen, true);
            }
          });
          return true;
        }
        onmove?.(uci);
        return true;
      }
      case INPUT_EVENT_TYPE.moveInputCanceled:
        board.removeMarkers(MARKER_TYPE.dot);
    }
  }
</script>

<div class="board" bind:this={el}></div>

<style>
  .board {
    width: 100%;
    max-width: min(640px, 100%);
    aspect-ratio: 1;
  }
</style>
