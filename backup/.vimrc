syntax on
set background=dark
set softtabstop=4
set tabstop=4
set shiftwidth=4
set expandtab
colorscheme desert

" execute pathogen#infect()

" Disable parentheses matching depends on system. This way we should address all cases (?)
set noshowmatch
" NoMatchParen " This doesnt work as it belongs to a plugin, which is only loaded _after_ all files are.
" Trying disable MatchParen after loading all plugins
"
function! g:Derp ()
    if exists(":NoMatchParen")
        :NoMatchParen
    endif
endfunction

augroup plugin_initialize
    autocmd!
    autocmd VimEnter * call Derp()
augroup END
